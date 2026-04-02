"""Persistent message queue: enqueue/dequeue for crash recovery."""

from daemon.globals import QUEUE_PATH, queue_lock
from daemon.utils import _read_json, _write_json


def load_queue() -> list:
    return _read_json(QUEUE_PATH, [])


def save_queue(queue: list):
    _write_json(QUEUE_PATH, queue)


def enqueue_message(msg: dict):
    """Add a message to the persistent queue."""
    with queue_lock:
        queue = load_queue()
        if not any(m.get("id") == msg.get("id") for m in queue):
            queue.append(msg)
            save_queue(queue)


def dequeue_message(msg_id):
    """Remove a message from the persistent queue after successful processing."""
    with queue_lock:
        queue = load_queue()
        queue = [m for m in queue if m.get("id") != msg_id]
        save_queue(queue)
