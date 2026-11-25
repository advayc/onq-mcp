# onQ MCP

A local MCP server for Queen’s University’s onQ (Brightspace). Ask your AI agent to read your course announcements, check your released grades, find course materials, or download a file.

## Install

Use Python 3.12 or later on macOS or Linux.

```sh
git clone https://github.com/advayc/onq-mcp.git
cd onq-mcp
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
```

For Codex, register the server with absolute paths and start a new session.

```sh
codex mcp add onq -- "$PWD/.venv/bin/python" "$PWD/onq_calendar.py"
```

For other MCP clients, set the stdio command to the full path to `.venv/bin/python` and add the full path to `onq_calendar.py` as its single argument.

## Connect your onQ account

```sh
.venv/bin/python onq_calendar.py --login
```

Complete Queen’s sign-in and MFA in the browser window. The window closes when the onQ home page loads. Run this command again when your session expires. The dedicated browser profile lives under `~/.config/onq-mcp/browser`.

For calendar deadlines, use **Calendar → Settings → Enable Calendar Feeds** in onQ, then **Subscribe → All Calendars and Tasks**. Copy the private subscription link. On macOS, run the following with the link on your clipboard. On Linux, pipe the link from your clipboard utility into the same Python command. [Queen’s calendar guide](https://www.queensu.ca/onqsupport/managing-your-courses-students#use-the-calendar-tool)

```sh
pbpaste | .venv/bin/python onq_calendar.py --set-feed
```

## Try it

- “Show my latest APSC announcements.”
- “What released grades can I see for APSC 143?”
- “Find the Week 11 lab sheet in course Content and download it.”
- “What’s due in the next two weeks?”

| Tool | What it does |
| --- | --- |
| `list_courses` | Find course names and IDs visible on the home page and in the course selector. |
| `read_course_announcements` | Read the visible text on the course’s Announcements page. |
| `read_my_grades` | Read the student Grades page and any feedback shown there. |
| `list_course_content` | Find topic titles and IDs in the course table of contents. Supply `module_id` to browse a listed classic module. |
| `read_content_topic` | Read a topic’s visible text, including same-site embedded pages. |
| `download_content_topic` | Save a file from the onQ topic’s Download control to `~/Downloads/onQ` and return its absolute path. |
| `upcoming_course_events` | Read upcoming dates from the optional calendar subscription. |

The signed-in tools use the student-facing pages, so course access, page layouts, and file permissions determine what they can retrieve. Use the `new_content` flag on content tools for the Lessons layout. Some externally hosted materials require opening the original topic in onQ. For documents such as PDFs or slides, download the file first and ask the agent to open it. [Queen’s Content guide](https://www.queensu.ca/onqsupport/content-students)

The server runs locally. Your AI client receives the course text you ask it to retrieve. Keep the browser profile, subscription link, and downloaded course material outside the repository. Visiting a topic can count as a view in Brightspace. File downloads use a numbered suffix if the filename already exists.

This is an independent project for Queen’s onQ.
