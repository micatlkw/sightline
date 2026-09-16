import asyncio
import pytest
from app.core.event_bus import EventBus


@pytest.mark.asyncio
async def test_event_bus_pub_sub():
    bus = EventBus()
    queue = bus.subscribe("detection")

    payload = {"id": 1, "camera_name": "front"}
    bus.publish("detection", payload)

    received = await asyncio.wait_for(queue.get(), timeout=1.0)
    assert received == payload

    bus.unsubscribe(queue)
    bus.publish("detection", {"id": 2})

    # Queue should be empty / unlinked
    assert queue.empty()


@pytest.mark.asyncio
async def test_event_bus_multiple_subscribers():
    bus = EventBus()
    q1 = bus.subscribe("detection")
    q2 = bus.subscribe("detection")
    q3 = bus.subscribe("other_topic")

    payload = {"msg": "hello"}
    bus.publish("detection", payload)

    r1 = await asyncio.wait_for(q1.get(), timeout=1.0)
    r2 = await asyncio.wait_for(q2.get(), timeout=1.0)
    assert r1 == payload
    assert r2 == payload
    assert q3.empty()


@pytest.mark.asyncio
async def test_event_bus_queue_full_non_blocking():
    bus = EventBus()
    small_q = bus.subscribe("detection", maxsize=1)

    bus.publish("detection", {"count": 1})
    bus.publish("detection", {"count": 2})  # should not block or crash

    first = await asyncio.wait_for(small_q.get(), timeout=1.0)
    assert first == {"count": 1}
