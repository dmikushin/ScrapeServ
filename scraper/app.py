from flask import Flask, request, jsonify
import sys
import os
from dotenv import load_dotenv
import ipaddress
import socket
from urllib.parse import urlparse
import json
import mimetypes
from PIL import Image
import tempfile
from worker import scrape_task, MAX_BROWSER_DIM, MIN_BROWSER_DIM, DEFAULT_BROWSER_DIM, DEFAULT_WAIT, MAX_SCREENSHOTS, MAX_WAIT, DEFAULT_SCREENSHOTS


app = Flask(__name__)

"""
Flask server runs and gets requests to scrape.
The server worker process spawned by gunicorn itself maintains a separate pool of scraping workers.
Upon a request to /scrape, the gunicorn worker asks the pool for a process to run a scrape, which spawns an isolated browser context.
The scrape workers' memory usage and number are limited by constants set in worker.py.

Now also joins multiple screenshots vertically into a single image before returning to the client.
"""

# For optional API key
load_dotenv()  # Load in API keys
SCRAPER_API_KEYS = [value for key, value in os.environ.items() if key.startswith('SCRAPER_API_KEY')]


@app.route('/')
def home():
    return "A rollicking band of pirates we, who tired of tossing on the sea, are trying our hands at burglary, with weapons grim and gory."


def is_private_ip(ip_str: str) -> bool:
    """
    Checks if the given IP address string (e.g., '10.0.0.1', '127.0.0.1')
    is private, loopback, or link-local.
    """
    try:
        ip_obj = ipaddress.ip_address(ip_str)
        return (
            ip_obj.is_loopback or
            ip_obj.is_private or
            ip_obj.is_reserved or
            ip_obj.is_link_local or
            ip_obj.is_multicast
        )
    except ValueError:
        return True  # If it can't parse, treat as "potentially unsafe"


def url_is_safe(url: str, allowed_schemes=None) -> bool:
    if allowed_schemes is None:
        # By default, allow http(s) and file
        allowed_schemes = {"http", "https", "file"}

    # Parse the URL
    parsed = urlparse(url.strip())
    scheme = parsed.scheme.lower()

    # Special handling for file:// URLs
    if scheme == "file":
        # You can add additional safety checks for file paths here if needed
        # For example, restrict to certain directories
        return True

    netloc = parsed.netloc.split(':')[0]  # extract host portion w/o port
    if scheme not in allowed_schemes:
        print(f"URL blocked: scheme '{scheme}' is not allowed.", file=sys.stderr)
        return False

    try:
        # Resolve the domain name to IP addresses
        # This can raise socket.gaierror if domain does not exist
        addrs = socket.getaddrinfo(netloc, None)
    except socket.gaierror:
        print(f"URL blocked: cannot resolve domain {netloc}", file=sys.stderr)
        return False

    # Check each resolved address
    for addrinfo in addrs:
        ip_str = addrinfo[4][0]
        if is_private_ip(ip_str):
            print(f"URL blocked: IP {ip_str} for domain {netloc} is private/loopback/link-local.", file=sys.stderr)
            return False

    # If all resolved IPs appear safe, pass it
    return True


# Includes dot
def get_ext_from_content_type(content_type: str):
    mime_type = content_type.split(';')[0].strip()
    extensions = mimetypes.guess_all_extensions(mime_type)
    if len(extensions):
        return f"{extensions[0]}"
    return ""


def stack_images_vertically(image_files, output_format):
    """
    Stack multiple images vertically into a single image.

    Args:
        image_files (list): List of paths to image files
        output_format (str): Output image format (jpeg, png, webp)

    Returns:
        str: Path to the stacked image
    """
    if not image_files:
        return None

    # If only one screenshot, just return it directly
    if len(image_files) == 1:
        return image_files[0]

    # Open all images
    images = [Image.open(img_file) for img_file in image_files]

    # Calculate the width of the final image (max width of all images)
    max_width = max(img.width for img in images)

    # Calculate the total height
    total_height = sum(img.height for img in images)

    # Create a new blank image with the calculated dimensions
    stacked_image = Image.new('RGB', (max_width, total_height))

    # Paste each image one below the other
    y_offset = 0
    for img in images:
        stacked_image.paste(img, (0, y_offset))
        y_offset += img.height
        img.close()  # Close the image file

    # Create a temporary file for the stacked image
    fd, output_path = tempfile.mkstemp(suffix=f'.{output_format}')
    os.close(fd)

    # Save the stacked image
    stacked_image.save(output_path, format=output_format.upper())

    return output_path


@app.route('/scrape', methods=('POST',))
def scrape():
    if len(SCRAPER_API_KEYS):
        auth_header = request.headers.get('Authorization')
        if auth_header is None:
            return jsonify({"error": "Authorization header is missing"}), 401

        if not auth_header.startswith('Bearer '):
            return jsonify({"error": "Invalid authorization header format"}), 401

        user_key = auth_header.split(' ')[1]
        if user_key not in SCRAPER_API_KEYS:
            return jsonify({'error': 'Invalid API key'}), 401

    url = request.json.get('url')

    if not url:
        return jsonify({'error': 'No URL provided'}), 400
    if not url_is_safe(url):
        return jsonify({'error': 'URL was judged to be unsafe'}), 400

    wait = request.json.get('wait', DEFAULT_WAIT)
    n_screenshots = request.json.get('max_screenshots', DEFAULT_SCREENSHOTS)
    browser_dim = request.json.get('browser_dim', DEFAULT_BROWSER_DIM)

    if wait < 0 or wait > MAX_WAIT:
        return jsonify({
            'error': f'Value {wait} for "wait" is unacceptable; must be between 0 and {MAX_WAIT}'
        }), 400

    for i, name in enumerate(['width', 'height']):
        if browser_dim[i] > MAX_BROWSER_DIM[i] or browser_dim[i] < MIN_BROWSER_DIM[i]:
            return jsonify({
                'error': f'Value {browser_dim[i]} for browser {name} is unacceptable; must be between {MIN_BROWSER_DIM[i]} and {MAX_BROWSER_DIM[i]}'
            }), 400

    if n_screenshots > MAX_SCREENSHOTS:
        return jsonify({
                'error': f'Value {n_screenshots} for max_screenshots is unacceptable; must be below {MAX_SCREENSHOTS}'
            }), 400

    # Determine the image format from the Accept header
    accept_header = request.headers.get('Accept', 'image/jpeg')
    accepted_formats = {
        'image/webp': 'webp',
        'image/png': 'png',
        'image/jpeg': 'jpeg',
        'image/*': 'jpeg',
        '*/*': 'jpeg'
    }

    image_format = accepted_formats.get(accept_header)
    if not image_format:
        accepted_formats_list = ', '.join(accepted_formats.keys())
        return jsonify({
            'error': f'Unsupported image format in Accept header ({accept_header}). Supported Accept header values are: {accepted_formats_list}'
        }), 406

    content_file = None
    try:
        status, headers, content_file, screenshot_files, metadata = scrape_task.apply_async(
            args=[url, wait, image_format, n_screenshots, browser_dim], kwargs={}
        ).get(timeout=60)  # 60 seconds
        headers = {str(k).lower(): v for k, v in headers.items()}  # make headers all lowercase (they're case insensitive)
    except Exception as e:
        # If scrape_in_child uses too much memory, it seems to end up here.
        # however, if exit(0) is called, I find it doesn't.
        print(f"Exception raised from scraping process: {e}", file=sys.stderr, flush=True)
        return jsonify({'error': 'Scraping process failed'}), 500

    successful = True if content_file else False

    if successful:
        # Join screenshots into a single image
        stacked_screenshot = None
        if screenshot_files:
            stacked_screenshot = stack_images_vertically(screenshot_files, image_format)

        boundary = 'Boundary712sAM12MVaJff23NXJ'

        # Store file paths for cleanup in the response object
        cleanup_files = []
        if content_file:
            cleanup_files.append(content_file)
        if stacked_screenshot:
            cleanup_files.append(stacked_screenshot)
        # Don't add screenshot_files to cleanup at this point

        # Generate a mixed multipart response
        def stream():
            try:
                # Start with headers and status as json
                # JSON part with filename
                filename = "info.json"
                yield (
                    f"--{boundary}\r\n"
                    "Content-Type: application/json\r\n"
                    f"Content-Disposition: attachment; name=\"{filename}\"; filename=\"{filename}\"\r\n\r\n"
                ).encode()
                yield json.dumps({'status': status, 'headers': headers, 'metadata': metadata}).encode()

                # Main content (HTML/other)
                ext = get_ext_from_content_type(headers['content-type'])
                filename = f"main{ext}"
                yield (
                    f"\r\n--{boundary}\r\n"
                    f"Content-Disposition: attachment; name=\"{filename}\"; filename=\"{filename}\"\r\n"
                    "Content-Transfer-Encoding: binary\r\n"
                    f"Content-Type: {headers['content-type']}\r\n\r\n"
                ).encode()
                with open(content_file, 'rb') as content:
                    while chunk := content.read(4096):
                        yield chunk

                # Combined screenshot (one file instead of many)
                if stacked_screenshot:
                    filename = f"screenshot.{image_format}"
                    yield (
                        f"\r\n--{boundary}\r\n"
                        f"Content-Disposition: attachment; name=\"{filename}\"; filename=\"{filename}\"\r\n"
                        "Content-Transfer-Encoding: binary\r\n"
                        f"Content-Type: image/{image_format}\r\n\r\n"
                    ).encode()
                    with open(stacked_screenshot, 'rb') as content:
                        while chunk := content.read(4096):
                            yield chunk

                # Final boundary
                yield f"\r\n--{boundary}--\r\n".encode()

                # After streaming is complete, now it's safe to clean up the original screenshots
                for file_path in screenshot_files:
                    if os.path.exists(file_path):
                        os.unlink(file_path)
            except Exception as e:
                print(f"Error during streaming: {e}", file=sys.stderr)

            finally:
                # Clean up files after stream is complete
                for file_path in cleanup_files:
                    try:
                        if os.path.exists(file_path):
                            os.unlink(file_path)
                    except Exception as e:
                        print(f"Error cleaning up file {file_path}: {e}", file=sys.stderr)

        # Return the streaming response
        response = app.response_class(
            stream(),
            status=200,
            mimetype=f'multipart/mixed; boundary={boundary}'
        )

        return response
    else:
        return jsonify({
            'error': "This is a generic error message; sorry about that."
        }), 500
