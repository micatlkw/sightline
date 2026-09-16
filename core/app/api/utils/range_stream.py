from pathlib import Path
from fastapi import HTTPException, Request, Response, status
from fastapi.responses import FileResponse, StreamingResponse


def range_stream_response(file_path: Path, request: Request) -> Response:
    """
    Serves a video file with HTTP 206 Partial Content (Range request) support
    enabling smooth timeline scrubbing and seeking in Android ExoPlayer.
    """
    if not file_path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video file not found")

    file_size = file_path.stat().st_size
    range_header = request.headers.get("range") or request.headers.get("Range")

    if not range_header or "=" not in range_header:
        return FileResponse(
            file_path,
            media_type="video/mp4",
            headers={"Accept-Ranges": "bytes", "Cache-Control": "private, max-age=1800"},
        )

    try:
        unit, range_val = range_header.split("=", 1)
        if unit.strip().lower() != "bytes":
            return FileResponse(
                file_path,
                media_type="video/mp4",
                headers={"Accept-Ranges": "bytes", "Cache-Control": "private, max-age=1800"},
            )

        range_parts = range_val.split("-", 1)
        start_str = range_parts[0].strip()
        end_str = range_parts[1].strip() if len(range_parts) > 1 else ""

        if start_str and end_str:
            start = int(start_str)
            end = int(end_str)
        elif start_str:
            start = int(start_str)
            end = file_size - 1
        elif end_str:
            start = max(0, file_size - int(end_str))
            end = file_size - 1
        else:
            start = 0
            end = file_size - 1

        if start >= file_size or end >= file_size or start > end:
            raise HTTPException(
                status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
                detail="Requested range not satisfiable",
                headers={"Content-Range": f"bytes */{file_size}"},
            )

        content_length = end - start + 1
        chunk_size = 64 * 1024  # 64 KB chunk size

        def iter_file():
            with open(file_path, "rb") as f:
                f.seek(start)
                bytes_left = content_length
                while bytes_left > 0:
                    read_len = min(chunk_size, bytes_left)
                    chunk = f.read(read_len)
                    if not chunk:
                        break
                    bytes_left -= len(chunk)
                    yield chunk

        headers = {
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(content_length),
            "Content-Type": "video/mp4",
            "Cache-Control": "private, max-age=1800",
        }
        return StreamingResponse(iter_file(), status_code=status.HTTP_206_PARTIAL_CONTENT, headers=headers)
    except (ValueError, IndexError):
        return FileResponse(
            file_path,
            media_type="video/mp4",
            headers={"Accept-Ranges": "bytes", "Cache-Control": "private, max-age=1800"},
        )
