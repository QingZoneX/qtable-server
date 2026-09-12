import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_attachment_upload_pending_grace_rejects_immediate_cleanup_window():
    with pytest.raises(ValidationError):
        Settings(ATTACHMENT_UPLOAD_PENDING_GRACE_SECONDS=59)

    configured = Settings(ATTACHMENT_UPLOAD_PENDING_GRACE_SECONDS=60)
    assert configured.ATTACHMENT_UPLOAD_PENDING_GRACE_SECONDS == 60
