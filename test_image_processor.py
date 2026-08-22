import io
import os
import unittest
from unittest.mock import patch

from PIL import Image

import image_processor
from http_requests import IMAGE_REQUEST_HEADERS, MAX_TRANSIENT_HTTP_ATTEMPTS


class FakeImageResponse:
    def __init__(self, content, content_type="image/jpeg", status_code=200, content_length=None):
        self.status_code = status_code
        self.headers = {"Content-Type": content_type}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)
        self.content = content
        self.closed = False

    def iter_content(self, chunk_size):
        for offset in range(0, len(self.content), chunk_size):
            yield self.content[offset:offset + chunk_size]

    def close(self):
        self.closed = True


def image_bytes(image_format):
    buffer = io.BytesIO()
    Image.new("RGB", (16, 16), "white").save(buffer, format=image_format)
    return buffer.getvalue()


class TestImageDownloadSafety(unittest.TestCase):
    def download_with_response(self, response):
        with patch("image_processor.requests.get", return_value=response), \
                patch("http_requests.time.sleep"):
            return image_processor.download_image("https://example.com/image")

    def assert_downloaded_image_is_removed(self, path):
        self.assertIsNotNone(path)
        self.assertTrue(os.path.exists(path))
        os.unlink(path)

    def test_valid_jpeg_is_accepted(self):
        response = FakeImageResponse(image_bytes("JPEG"))

        path = self.download_with_response(response)

        self.assert_downloaded_image_is_removed(path)
        self.assertTrue(response.closed)

    def test_image_request_uses_explicit_image_headers(self):
        response = FakeImageResponse(image_bytes("JPEG"))

        with patch("image_processor.requests.get", return_value=response) as get, \
                patch("http_requests.time.sleep"):
            path = image_processor.download_image("https://example.com/image")

        self.assert_downloaded_image_is_removed(path)
        self.assertEqual(get.call_args.kwargs["headers"], IMAGE_REQUEST_HEADERS)

    def test_transient_403_is_retried_before_a_successful_image_download(self):
        blocked = FakeImageResponse(b"", status_code=403)
        success = FakeImageResponse(image_bytes("JPEG"))

        with patch("image_processor.requests.get", side_effect=[blocked, success]) as get, \
                patch("http_requests.time.sleep") as sleep:
            path = image_processor.download_image("https://example.com/image")

        self.assert_downloaded_image_is_removed(path)
        self.assertEqual(get.call_count, 2)
        sleep.assert_called_once()

    def test_persistent_403_fails_cleanly_after_bounded_retries(self):
        responses = [FakeImageResponse(b"", status_code=403) for _ in range(MAX_TRANSIENT_HTTP_ATTEMPTS)]

        with patch("image_processor.requests.get", side_effect=responses) as get, \
                patch("http_requests.time.sleep") as sleep:
            path = image_processor.download_image("https://example.com/image")

        self.assertIsNone(path)
        self.assertEqual(get.call_count, MAX_TRANSIENT_HTTP_ATTEMPTS)
        self.assertEqual(sleep.call_count, MAX_TRANSIENT_HTTP_ATTEMPTS - 1)

    def test_valid_png_is_accepted(self):
        response = FakeImageResponse(image_bytes("PNG"), content_type="image/png")

        path = self.download_with_response(response)

        self.assert_downloaded_image_is_removed(path)

    def test_html_response_is_rejected(self):
        response = FakeImageResponse(b"<html>not an image</html>", content_type="text/html")

        path = self.download_with_response(response)

        self.assertIsNone(path)

    def test_http_error_is_rejected(self):
        response = FakeImageResponse(image_bytes("JPEG"), status_code=500)

        path = self.download_with_response(response)

        self.assertIsNone(path)

    def test_download_size_limit_is_enforced(self):
        response = FakeImageResponse(
            b"x" * (image_processor.MAX_IMAGE_DOWNLOAD_BYTES + 1),
            content_length=image_processor.MAX_IMAGE_DOWNLOAD_BYTES + 1,
        )

        path = self.download_with_response(response)

        self.assertIsNone(path)

    def test_streaming_download_size_limit_is_enforced(self):
        response = FakeImageResponse(b"x" * (image_processor.MAX_IMAGE_DOWNLOAD_BYTES + 1))

        path = self.download_with_response(response)

        self.assertIsNone(path)

    def test_corrupt_image_data_is_rejected(self):
        response = FakeImageResponse(b"not a real jpeg")

        path = self.download_with_response(response)

        self.assertIsNone(path)


if __name__ == "__main__":
    unittest.main()
