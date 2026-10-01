"""
OSS Upload Utility

Provides a simple interface to upload files to Aliyun OSS.
"""
import os
import re
import uuid

from ..core.runtime import get_config

_FILENAME_STRIP_RE = re.compile(r"[^A-Za-z0-9_.-]")


def _secure_filename(filename: str) -> str:
    """替代原 werkzeug.utils.secure_filename：
    去路径分隔符，仅保留字母数字与 .-_，剔除首尾的点和下划线。"""
    if not filename:
        return ""
    # 统一斜杠并取最后一段（同时处理 Windows 反斜杠与盘符路径）
    filename = filename.replace("\\", "/").split("/")[-1]
    filename = _FILENAME_STRIP_RE.sub("", filename).strip("._")
    return filename or "file"


def upload_to_oss(file_obj, folder='avatars'):
    """
    Upload a file-like object to Aliyun OSS.

    Args:
        file_obj: The file-like object holding the upload
            (e.g., the parsed upload from the HTTP layer)
        folder: The target folder in OSS bucket

    Returns:
        tuple: (success, url_or_error_message)
    """
    endpoint = get_config().get('OSS_ENDPOINT')
    access_key_id = get_config().get('OSS_ACCESS_KEY_ID')
    access_key_secret = get_config().get('OSS_ACCESS_KEY_SECRET')
    bucket_name = get_config().get('OSS_BUCKET_NAME')
    domain = get_config().get('OSS_DOMAIN')

    if not all([endpoint, access_key_id, access_key_secret, bucket_name]):
        return (
            False,
            "OSS configuration is missing "
            "(endpoint, access_key_id, access_key_secret, or bucket_name)",
        )

    try:
        import oss2

        original_filename = secure_filename(file_obj.filename)
        ext = os.path.splitext(original_filename)[1]
        unique_filename = f"{uuid.uuid4().hex}{ext}"
        object_name = f"{folder}/{unique_filename}"

        auth = oss2.Auth(access_key_id, access_key_secret)
        bucket = oss2.Bucket(auth, endpoint, bucket_name)

        file_obj.seek(0)
        bucket.put_object(object_name, file_obj.read())

        if domain:
            url = f"{domain.rstrip('/')}/{object_name}"
        else:
            protocol = 'https://' if not endpoint.startswith('http') else ''
            endpoint_clean = (
                endpoint.replace('https://', '').replace('http://', '')
            )
            url = f"{protocol}{bucket_name}.{endpoint_clean}/{object_name}"

        return True, url

    except ModuleNotFoundError as e:
        return False, f"OSS dependency is missing: {str(e)}"
    except Exception as e:
        return False, f"Failed to upload to OSS: {str(e)}"
