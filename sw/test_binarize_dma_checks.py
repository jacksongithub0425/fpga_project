"""Host-only safety tests for the binarizer DMA readiness gate."""

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sw import binarize_dma_checks as checks


class FakeMmio:
    def __init__(self, control: int, status):
        self.control = control
        self.statuses = list(status) if isinstance(status, (list, tuple)) else [status]
        self.status_reads = 0

    def read(self, offset: int) -> int:
        if offset in (0x00, 0x30):
            return self.control
        if offset in (0x04, 0x34):
            index = min(self.status_reads, len(self.statuses) - 1)
            self.status_reads += 1
            return self.statuses[index]
        raise AssertionError(f"unexpected MMIO read at 0x{offset:x}")


def channel(
    status,
    *,
    control: int = 0x00010003,
    offset: int = 0,
    first_transfer=True,
    active_buffer=None,
):
    return SimpleNamespace(
        _mmio=FakeMmio(control, status),
        _offset=offset,
        _first_transfer=first_transfer,
        _active_buffer=active_buffer,
    )


def dma(send, recv=None):
    return SimpleNamespace(
        sendchannel=send,
        recvchannel=recv or channel(0x2, offset=0x30),
    )


class IdleCore:
    @staticmethod
    def read(_offset: int) -> int:
        return 0x4


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, duration):
        self.now += max(float(duration), 0.001)


class DmaReadyTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.monotonic_patch = patch.object(
            checks.time, "monotonic", self.clock.monotonic)
        self.sleep_patch = patch.object(checks.time, "sleep", self.clock.sleep)
        self.monotonic_patch.start()
        self.sleep_patch.start()

    def tearDown(self):
        self.sleep_patch.stop()
        self.monotonic_patch.stop()

    def test_accepts_architected_virgin_non_idle_state(self):
        send = channel(0x0, first_transfer=True, active_buffer=None)
        recv = channel(
            0x0, offset=0x30, first_transfer=True, active_buffer=None)
        checks._assert_dma_ready(IdleCore(), dma(send, recv), 1.0)

    def test_accepts_normal_idle_state(self):
        checks._assert_dma_ready(
            IdleCore(), dma(channel(0x2, first_transfer=False)), 1.0)

    def test_polls_non_first_channel_until_idle(self):
        send = channel([0x0, 0x0, 0x2], first_transfer=False)
        checks._assert_dma_ready(IdleCore(), dma(send), 1.0)
        self.assertGreaterEqual(send._mmio.status_reads, 3)

    def test_rejects_non_first_busy_state(self):
        with self.assertRaises(TimeoutError):
            checks._assert_dma_ready(
                IdleCore(), dma(channel(0x0, first_transfer=False)), 0.003)

    def test_rejects_virgin_marker_with_active_buffer(self):
        with self.assertRaises(TimeoutError):
            checks._assert_dma_ready(
                IdleCore(),
                dma(channel(0x0, first_transfer=True, active_buffer=object())),
                0.003,
            )

    def test_rejects_halted_or_run_disabled_state(self):
        with self.assertRaises(TimeoutError):
            checks._assert_dma_ready(
                IdleCore(), dma(channel(0x1, control=0x00010002)), 0.003)

    def test_rejects_dma_error_immediately(self):
        with self.assertRaisesRegex(RuntimeError, "DMA error before start"):
            checks._assert_dma_ready(IdleCore(), dma(channel(0x10)), 1.0)

    def test_rejects_missing_private_first_transfer_proof(self):
        send = channel(0x0)
        del send._first_transfer
        with self.assertRaisesRegex(TimeoutError, "first_transfer=<missing>"):
            checks._assert_dma_ready(IdleCore(), dma(send), 0.003)


if __name__ == "__main__":
    unittest.main()
