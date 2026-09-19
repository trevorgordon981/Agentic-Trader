from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "ops" / "sync_fidelity_exports.sh"


def test_transport_pins_one_authorized_agent_identity() -> None:
    text = SCRIPT.read_text()
    assert "-o IdentitiesOnly=yes" in text
    assert '-o IdentityFile="$SSH_IDENTITY"' in text
    assert "RSYNC_RSH=" in text
    assert text.count('-e "$RSYNC_RSH"') >= 2
    assert text.count('ssh "${SSH_OPTS[@]}" "$REMOTE"') >= 2


def test_primary_transfer_retries_then_fails_loudly() -> None:
    text = SCRIPT.read_text()
    assert "for attempt in 1 2 3" in text
    assert "if ! sync_primary; then" in text
    assert 'echo "FATAL: rsync to $REMOTE failed"' in text
