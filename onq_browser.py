"""Read student-facing onQ pages in a dedicated, locally signed-in browser."""

import asyncio
import fcntl
import json
import os
import re
import shutil
import tempfile
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from threading import Lock
from typing import Annotated
from urllib.parse import parse_qs, urlsplit

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from playwright.sync_api import Error as BrowserError
from playwright.sync_api import Page, sync_playwright
from pydantic import Field


ORIGIN = "https://onq.queensu.ca"
PROFILE = Path.home() / ".config" / "onq-mcp" / "browser"
SESSION = PROFILE / "onq-session.json"
DOWNLOADS = Path.home() / "Downloads" / "onQ"
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)
BROWSER_LOCK = Lock()
CourseId = Annotated[int, Field(gt=0, le=2147483647, description="onQ course ID from list_courses.")]
TopicId = Annotated[int, Field(gt=0, le=2147483647, description="onQ topic ID from list_course_content.")]
Offset = Annotated[int, Field(ge=0, description="Use next_offset from the previous result to continue.")]
DOWNLOAD_NAME = re.compile(r"^download(?:\s+(?:file|topic|original)(?:\s+file)?)?$", re.I)


class OnQError(Exception):
    """A message safe to show to the MCP client."""


def browser_tool(function):
    @wraps(function)
    async def run(**arguments):
        def invoke():
            with BROWSER_LOCK:
                return function(**arguments)

        return await asyncio.to_thread(invoke)

    return run


@contextmanager
def browser(*, interactive: bool = False):
    if not interactive and not SESSION.is_file():
        raise OnQError("Run onq_calendar.py --login to sign in to onQ first.")

    PROFILE.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (PROFILE / "profile.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise OnQError("The onQ browser is busy. Close the sign-in window or retry shortly.") from None

        try:
            with sync_playwright() as playwright:
                chromium = playwright.chromium.launch(headless=not interactive)
                context = chromium.new_context(
                    storage_state=None if interactive else SESSION, accept_downloads=True, locale="en-CA"
                )
                try:
                    page = context.new_page()
                    page.set_default_timeout(8000)
                    page.set_default_navigation_timeout(20000)
                    yield page
                    if same_site(page.url):
                        save_session(context.storage_state())
                finally:
                    context.close()
                    chromium.close()
        except BrowserError:
            raise OnQError(
                "The browser could not complete the onQ request. Check Chromium is installed "
                "with python -m playwright install chromium. Re-run --login if your session expired."
            ) from None


def save_session(state: dict) -> None:
    session = {
        "cookies": [cookie for cookie in state["cookies"] if cookie["domain"].lstrip(".") == "onq.queensu.ca"],
        "origins": [origin for origin in state["origins"] if origin["origin"] == ORIGIN],
    }
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=PROFILE, delete=False) as pending:
        path = Path(pending.name)
        try:
            json.dump(session, pending)
            pending.close()
            os.replace(path, SESSION)
        finally:
            path.unlink(missing_ok=True)


def same_site(url: str) -> bool:
    parsed = urlsplit(url)
    return parsed.scheme == "https" and parsed.netloc.lower() == "onq.queensu.ca"


def navigate(page: Page, path: str) -> None:
    response = page.goto(ORIGIN + path, wait_until="domcontentloaded")
    require_onq(page)
    if response is None or response.status >= 400:
        raise OnQError("onQ could not open this page with your current course access.")


def require_onq(page: Page) -> None:
    final_path = urlsplit(page.url).path.lower()
    if not same_site(page.url) or any(part in final_path for part in ("/login", "/saml", "/shibboleth")):
        raise OnQError("Your onQ session needs sign-in. Run onq_calendar.py --login.")


def login() -> None:
    with browser(interactive=True) as page:
        page.goto(ORIGIN + "/d2l/home", wait_until="domcontentloaded")
        print("Sign in to Queen's onQ in the opened browser.", flush=True)
        try:
            page.wait_for_url(re.compile(r"^https://onq\.queensu\.ca/d2l/home(?:[/?#]|$)"), timeout=180000)
        except BrowserError:
            raise OnQError("Sign-in was incomplete. Run --login again and land on the onQ home page.") from None
        print("onQ browser sign-in saved locally.")


def main_area(page: Page):
    for selector in ("main", "[role=main]", ".d2l-page-main", "[id=d2l_body_main]"):
        area = page.locator(selector).first
        if area.is_visible():
            return area
    return page.locator("body")


def page_text(page: Page, offset: int, *, include_frames: bool = False) -> dict:
    require_onq(page)
    body = main_area(page).inner_text().strip()
    if include_frames:
        for frame in page.main_frame.child_frames:
            if same_site(frame.url) and frame.locator("body").is_visible():
                body += "\n\n" + frame.locator("body").inner_text().strip()
    end = offset + 14000
    return {"text": body[offset:end], "next_offset": end if end < len(body) else None}


def visible_links(page: Page, selector: str) -> list[dict]:
    return page.locator(selector).evaluate_all(
        """links => links.filter(link => link.getClientRects().length).map(link => ({
            url: link.href,
            title: (link.getAttribute('aria-label') || link.innerText
                    || link.getAttribute('title') || '').trim()
        }))"""
    )


def courses_on_page(page: Page) -> list[dict]:
    selector = "a[href*='/d2l/home/']"
    courses = {}
    for link in visible_links(page, selector):
        if not same_site(link["url"]) or not link["title"]:
            continue
        match = re.fullmatch(r"/d2l/home/(\d+)/?", urlsplit(link["url"]).path)
        if match:
            course_id = int(match[1])
            courses[course_id] = {"course_id": course_id, "title": link["title"]}
    return list(courses.values())


def content_links(page: Page, course_id: int, query: str) -> list[dict]:
    selector = "a[href*='/viewContent/'], a[href*='/topics/'], a[href*='/lessons/']"
    topic_paths = (
        re.compile(rf"/d2l/le/content/{course_id}/viewContent/(\d+)/View/?", re.I),
        re.compile(rf"/d2l/le/lessons/{course_id}/(?:topics/)?(\d+)/?", re.I),
    )
    results = {}
    for link in visible_links(page, selector):
        if not same_site(link["url"]) or query.casefold() not in link["title"].casefold():
            continue
        path = urlsplit(link["url"]).path
        for pattern in topic_paths:
            match = pattern.fullmatch(path)
            if match and link["title"]:
                topic_id = int(match[1])
                results[topic_id] = {"topic_id": topic_id, "title": link["title"], "url": ORIGIN + path}
                break
    return list(results.values())


def classic_modules(page: Page, course_id: int) -> list[dict]:
    modules = {}
    for link in visible_links(page, "a[href*='ModuleCO-'], a[href*='moduleId=']"):
        if not same_site(link["url"]) or not link["title"]:
            continue
        url = urlsplit(link["url"])
        if url.path.rstrip("/").lower() != f"/d2l/le/content/{course_id}/home":
            continue
        fields = parse_qs(url.query)
        fragment_fields = parse_qs(url.fragment.partition("?")[2])
        if "itemIdentifier" in fields:
            match = re.fullmatch(r"D2L\.LE\.Content\.ContentObject\.ModuleCO-(\d+)", fields["itemIdentifier"][0])
        else:
            values = fragment_fields.get("moduleId", fields.get("moduleId", [""]))
            match = re.fullmatch(r"(\d+)", values[0])
        if match:
            module_id = int(match[1])
            modules[module_id] = {"module_id": module_id, "title": link["title"], "url": link["url"]}
    return list(modules.values())


def topic_path(course_id: int, topic_id: int, new_content: bool) -> str:
    if new_content:
        return f"/d2l/le/lessons/{course_id}/topics/{topic_id}"
    return f"/d2l/le/content/{course_id}/viewContent/{topic_id}/View"


def save_download(source: Path, suggested_filename: str) -> Path:
    filename = re.sub(r"[\x00-\x1f\x7f/\\:]", "_", suggested_filename).strip(" .") or "course-file"
    filename = filename.encode("utf-8")[:180].decode("utf-8", errors="ignore").rstrip(" .")
    DOWNLOADS.mkdir(mode=0o700, parents=True, exist_ok=True)
    if DOWNLOADS.is_symlink():
        raise OnQError("The onQ download directory must be a regular directory.")
    stem, suffix = os.path.splitext(filename)
    with tempfile.TemporaryDirectory(dir=DOWNLOADS) as directory:
        pending = Path(directory) / "download"
        shutil.copyfile(source, pending)
        pending.chmod(0o600)
        for index in range(1000):
            name = filename if index == 0 else f"{stem} ({index}){suffix}"
            destination = DOWNLOADS / name
            try:
                os.link(pending, destination)
                return destination.resolve()
            except FileExistsError:
                continue
    raise OnQError("Too many files with this name in the onQ download directory.")


def register_tools(mcp: FastMCP) -> None:
    @mcp.tool(annotations=READ_ONLY)
    @browser_tool
    def list_courses() -> dict:
        """List course IDs and names visible in My Courses or the onQ course selector."""
        with browser() as page:
            navigate(page, "/d2l/home")
            courses = {course["course_id"]: course for course in courses_on_page(page)}
            selector = page.get_by_role("button", name=re.compile(r"select (a )?course|course selector", re.I))
            if selector.first.is_visible():
                selector.first.click()
                try:
                    page.locator("a[href*='/d2l/home/']").first.wait_for(state="visible", timeout=4000)
                except BrowserError:
                    pass
                courses.update({course["course_id"]: course for course in courses_on_page(page)})
            return {"courses": list(courses.values())}

    @mcp.tool(annotations=READ_ONLY)
    @browser_tool
    def read_course_announcements(course_id: CourseId, offset: Offset = 0) -> dict:
        """Read the visible Announcements page, including any dates and bodies shown by onQ."""
        with browser() as page:
            navigate(page, f"/d2l/lms/news/main.d2l?ou={course_id}")
            return page_text(page, offset)

    @mcp.tool(annotations=READ_ONLY)
    @browser_tool
    def read_my_grades(course_id: CourseId, offset: Offset = 0) -> dict:
        """Read your released grades and feedback visible on the student Grades page."""
        with browser() as page:
            navigate(page, f"/d2l/lms/grades/my_grades/main.d2l?ou={course_id}")
            return page_text(page, offset)

    @mcp.tool(annotations=READ_ONLY)
    @browser_tool
    def list_course_content(
        course_id: CourseId,
        query: Annotated[str, Field(max_length=200, description="Optional text to match in topic names.")] = "",
        offset: Offset = 0,
        new_content: bool = False,
        module_id: Annotated[int, Field(gt=0, le=2147483647)] | None = None,
    ) -> dict:
        """List visible topic links on a course's Content page. For the lessons layout, use new_content."""
        with browser() as page:
            path = f"/d2l/le/lessons/{course_id}" if new_content else f"/d2l/le/content/{course_id}/Home"
            navigate(page, path)
            new_content = urlsplit(page.url).path.startswith("/d2l/le/lessons/")
            if not new_content and module_id is None:
                toc = page.get_by_role("link", name="Table of Contents", exact=True).first
                if toc.is_visible():
                    toc.click()
                    page.wait_for_load_state("domcontentloaded")
            require_onq(page)
            modules = classic_modules(page, course_id) if not new_content else []
            if module_id is not None:
                module = next((item for item in modules if item["module_id"] == module_id), None)
                if module is None:
                    raise OnQError("This module is unavailable from the course's Content page.")
                navigate(page, module["url"].removeprefix(ORIGIN))
            topics = content_links(page, course_id, query)
            end = offset + 75
            return {
                "topics": topics[offset:end],
                "modules": [{"module_id": item["module_id"], "title": item["title"]} for item in modules],
                "next_offset": end if end < len(topics) else None,
                "content_page": ORIGIN + urlsplit(page.url).path,
                "new_content": new_content,
            }

    @mcp.tool(annotations=READ_ONLY)
    @browser_tool
    def read_content_topic(
        course_id: CourseId, topic_id: TopicId, offset: Offset = 0, new_content: bool = False
    ) -> dict:
        """Read visible text from a course topic. Use download_content_topic for file-based topics."""
        with browser() as page:
            navigate(page, topic_path(course_id, topic_id, new_content))
            result = page_text(page, offset, include_frames=True)
            return {**result, "topic_url": ORIGIN + urlsplit(page.url).path}

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True))
    @browser_tool
    def download_content_topic(course_id: CourseId, topic_id: TopicId, new_content: bool = False) -> dict:
        """Save a downloadable onQ topic to ~/Downloads/onQ and return the local path."""
        with browser() as page:
            navigate(page, topic_path(course_id, topic_id, new_content))
            area = main_area(page)
            control = area.get_by_role("button", name=DOWNLOAD_NAME).or_(
                area.get_by_role("link", name=DOWNLOAD_NAME)
            ).filter(visible=True).first
            try:
                control.wait_for(state="visible", timeout=8000)
                with page.expect_download(timeout=15000) as pending:
                    control.click()
                download = pending.value
                if not same_site(download.url):
                    raise OnQError("Open the topic in onQ to download files hosted by another service.")
                path = save_download(download.path(), download.suggested_filename)
            except BrowserError:
                raise OnQError("This topic did not offer a download through the onQ viewer.") from None
            return {"path": str(path), "filename": path.name, "bytes": path.stat().st_size}
