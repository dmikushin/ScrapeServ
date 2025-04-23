from celery import Celery
from playwright.sync_api import sync_playwright, Error as PlaywrightError
import resource
import math
import tempfile
import os
from PIL import Image
import sys
import mimetypes

# Server options
MEM_LIMIT_MB = 4_000  # 4 GB memory threshold for child scraping process
MAX_CONCURRENT_TASKS = 3
DEFAULT_SCREENSHOTS = 5  # The max number of screenshots if the user doesn't set a max
MAX_SCREENSHOTS = 10  # User cannot set max_screenshots above this value
DEFAULT_WAIT = 1000  # Value for wait if a user doesn't set one (ms)
MAX_WAIT = 5000  # A user cannot ask for more than this long of a wait (ms)
SCREENSHOT_QUALITY = 85  # Argument to PIL image save
DEFAULT_BROWSER_DIM = [1280, 2000]  # If a user doesn't set browser dimensions  Width x Height in pixels
MAX_BROWSER_DIM = [2400, 4000]  # Maximum width and height a user can set
MIN_BROWSER_DIM = [100, 100]  # Minimum width and height a user can set
USER_AGENT = "Mozilla/5.0 (compatible; Abbey/1.0; +https://github.com/US-Artificial-Intelligence/scraper)"

CELERY_RESULT_BACKEND = "redis://localhost:6379/0"
CELERY_BROKER_URL = "redis://localhost:6379/0"

def make_celery():
    celery = Celery(
        backend=CELERY_RESULT_BACKEND,
        broker=CELERY_BROKER_URL
    )
    celery.conf.update(
        worker_concurrency=MAX_CONCURRENT_TASKS,  # Limit number of concurrent tasks
    )

    return celery

celery = make_celery()

def guess_mime_type(file_path):
    """Guess MIME type based on file extension"""
    mime_type, _ = mimetypes.guess_type(file_path)
    if mime_type:
        return mime_type

    # Default fallbacks based on extension
    ext = os.path.splitext(file_path)[1].lower()
    if ext in ['.html', '.htm']:
        return 'text/html'
    elif ext == '.pdf':
        return 'application/pdf'
    elif ext == '.txt':
        return 'text/plain'
    elif ext in ['.jpg', '.jpeg']:
        return 'image/jpeg'
    elif ext == '.png':
        return 'image/png'

    # Default fallback
    return 'application/octet-stream'

@celery.task
def scrape_task(url, wait, image_format, n_screenshots, browser_dim):

    # Memory limits for the task process
    soft, hard = (MEM_LIMIT_MB * 1024 * 1024, MEM_LIMIT_MB * 1024 * 1024)
    resource.setrlimit(resource.RLIMIT_AS, (soft, hard))  # Browser should inherit this limit

    content_file_tmp = tempfile.NamedTemporaryFile(mode='w+b', delete=False)
    content_file = content_file_tmp.name

    raw_screenshot_files = []
    metadata = {
        'image_sizes': [],
        'original_screenshots_n': 0,
        'truncated_screenshots_n': 0,
    }
    status = None
    headers = None
    try:
        with sync_playwright() as p:
            # Should be resilient to untrusted websites
            browser = p.firefox.launch(headless=True, timeout=10_000)  # 10s startup timeout

            # Configure browser context based on URL type
            is_file_url = url.startswith("file://")

            # Special configuration for file:// URLs
            if is_file_url:
                context = browser.new_context(
                    viewport={"width": browser_dim[0], "height": browser_dim[1]},
                    accept_downloads=True,
                    user_agent=USER_AGENT,
                    bypass_csp=True  # Allow file access
                )
            else:
                context = browser.new_context(
                    viewport={"width": browser_dim[0], "height": browser_dim[1]},
                    accept_downloads=True,
                    user_agent=USER_AGENT
                )

            page = context.new_page()

            # Set various security headers and limits
            page.set_default_timeout(30000)  # 30 second timeout
            page.set_default_navigation_timeout(30000)

            processing_download = False

            REDIRECT_STATUS_CODES = [301, 302, 303, 307, 308]  # Some in the 300s like Multiple Choice not included

            # Handle response will work even if goto fails (i.e., for download).
            response = None

            # Special handling for file:// URLs (they don't generate normal responses)
            if is_file_url:
                try:
                    # For file URLs, we navigate without expecting a normal response
                    page.goto(url)

                    # For file URLs, create synthetic response information
                    status = 200  # Assume success
                    file_path = url[7:]  # Remove 'file://'
                    content_type = guess_mime_type(file_path)
                    headers = {
                        "content-type": content_type,
                        "source": "file"
                    }
                except PlaywrightError as e:
                    print(f"Error accessing file URL: {e}", file=sys.stderr)
                    raise e
            else:
                # Normal HTTP(S) URL handling
                def handle_response(_response):
                    nonlocal response
                    # Check if it's the main resource
                    if _response.url == url:
                        response = _response
                    # ...or we just got a 302 and are going to a new URL to scrape
                    elif response and response.status in REDIRECT_STATUS_CODES:
                        response = _response

                page.on("response", handle_response)

                # Navigate to the page
                # If there's no download, screenshotting and request stuff proceeds as normal
                # If a download is initiated, Playwright throws an error which you can then handle
                try:
                    response = page.goto(url)
                    if response and response.status in REDIRECT_STATUS_CODES:
                        response = page.goto(response.headers.get('location'))
                except PlaywrightError as e:
                    # Unfortunately, a specific error isn't thrown - have to use the substring
                    substr = "Download is starting"
                    if substr in str(e):
                        # If I use this around the first response, a timeout will occur when there's no download.
                        # But there's still the same exception to handle even with the expect download... playwright can be hell sometimes
                        with page.expect_download() as download_info:
                            try:
                                if response and response.status in REDIRECT_STATUS_CODES:
                                    loc = response.headers.get('location')
                                    page.goto(loc)
                                else:
                                    page.goto(url)
                            except PlaywrightError as e:
                                if substr in str(e):
                                    processing_download = True
                                    download = download_info.value
                                    download.save_as(content_file_tmp.name)
                                    # Note that this "response" isn't the one assigned in the try;
                                    # It's the one from handle_response
                                    status = response.status
                                    headers = response.headers
                                else:
                                    raise e
                    else:
                        raise e

                if not response:
                    raise Exception("Response was none")

                status = response.status
                headers = dict(response.headers) if response else {}
                content_type = headers.get("content-type", "").lower()

            # Common handling for both URL types
            if is_file_url or (not processing_download and status < 400):
                # If file URL or successful web URL
                content_type = headers.get("content-type", "").lower()

                # If this is an HTML page, take screenshots
                if "text/html" in content_type:
                    page.wait_for_timeout(wait)

                    # Get total page height
                    total_height = page.evaluate("() => document.documentElement.scrollHeight")

                    # Calculate number of segments needed
                    metadata['original_screenshots_n'] = math.ceil(total_height / browser_dim[1])
                    num_segments = min(metadata['original_screenshots_n'], n_screenshots)
                    metadata['truncated_screenshots_n'] = num_segments

                    raw_screenshot_files = []

                    for i in range(num_segments):
                        tmp = tempfile.NamedTemporaryFile(mode='w+b', delete=False)

                        start_y = i * browser_dim[1]
                        page.evaluate(f"window.scrollTo(0, {start_y})")
                        page.wait_for_timeout(wait)

                        page.screenshot(
                            path=tmp.name,
                            animations="disabled",
                            clip={
                                "x": 0,
                                "y": 0,
                                "width": browser_dim[0],
                                "height": browser_dim[1]
                            }
                        )

                        raw_screenshot_files.append(tmp.name)
                        tmp.close()

                # If not processing download, retrieve content
                if not processing_download:
                    if is_file_url:
                        # For file URLs, copy the original file directly
                        file_path = url[7:]  # Remove 'file://' prefix
                        try:
                            # Read the original file in binary mode
                            with open(file_path, 'rb') as original_file:
                                file_content = original_file.read()
                                # Write to our temp file
                                with open(content_file_tmp.name, 'wb') as f:
                                    f.write(file_content)
                        except Exception as e:
                            print(f"Error reading file {file_path}: {e}", file=sys.stderr)
                            raise e
                    else:
                        # For web URLs, get raw bytes
                        file_bytes = response.body()
                        content_file_tmp.write(file_bytes)

            browser.close()

    except Exception as e:
        for ss in raw_screenshot_files:
            os.remove(ss)
        content_file_tmp.close()
        os.remove(content_file)
        raise e

    # Compress the screenshot files
    compressed_screenshot_files = []
    for ss in raw_screenshot_files:
        try:
            tmp = tempfile.NamedTemporaryFile(mode='w+b', delete=False)
            o_size = os.path.getsize(ss)
            with Image.open(ss) as img:
                if img.mode == 'RGBA':  # Will throw an error unless converted
                    img = img.convert('RGB')
                img.save(tmp.name, image_format.upper(), quality=SCREENSHOT_QUALITY)
            c_size = os.path.getsize(tmp.name)
            metadata['image_sizes'] = {
                'original': o_size,
                'compressed': c_size
            }
            compressed_screenshot_files.append(tmp.name)
        except Exception as e:
            for css in compressed_screenshot_files:
                os.remove(css)
            content_file_tmp.close()
            os.remove(content_file)
            raise e
        finally:
            os.remove(ss)

    return status, headers, content_file, compressed_screenshot_files, metadata
