"""
F13: Hall Collaboration Timeline

Multi-agent discussion timeline with handoff and evidence threads.
Includes structured discussion cycles with role-ordered speaker queue
(pattern extracted from openclaw hall-speaker-policy.ts).
"""

import logging
import time
import uuid
from typing import Dict, List, Optional
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from src.core.bounded_history import BoundedHistory

logger = logging.getLogger("nexus-hall")


class EventType(Enum):
    MESSAGE = "message"
    HANDOFF = "handoff"
    EVIDENCE = "evidence"
    DECISION = "decision"
    QUESTION = "question"
    ANSWER = "answer"


class TaskStage(str, Enum):
    DRAFT = "draft"
    DISCUSSION = "discussion"
    EXECUTION = "execution"
    REVIEW = "review"
    DONE = "done"


ROLE_ORDER = ["planner", "coder", "reviewer", "manager"]


@dataclass
class DiscussionCycle:
    cycle_id: str = ""
    opened_at: str = ""
    opened_by: str = ""
    expected_participant_ids: List[str] = field(default_factory=list)
    completed_participant_ids: List[str] = field(default_factory=list)

    def __post_init__(self):
        if not self.cycle_id:
            self.cycle_id = str(uuid.uuid4())[:8]
        if not self.opened_at:
            self.opened_at = datetime.now().isoformat()


@dataclass
class ExecutionLock:
    locked: bool = False
    locked_by: str = ""
    locked_at: str = ""
    token: str = ""

    def acquire(self, agent: str) -> bool:
        if self.locked:
            return False
        self.locked = True
        self.locked_by = agent
        self.locked_at = datetime.now().isoformat()
        self.token = str(uuid.uuid4())[:8]
        return True

    def release(self, token: str) -> bool:
        if not self.locked:
            return False
        if self.token and self.token != token:
            return False
        self.locked = False
        self.locked_by = ""
        self.locked_at = ""
        self.token = ""
        return True


@dataclass
class TaskCard:
    task_id: str = ""
    title: str = ""
    description: str = ""
    stage: TaskStage = TaskStage.DRAFT
    assigned_to: str = ""
    discussion_cycle: Optional[DiscussionCycle] = None
    execution_lock: Optional[ExecutionLock] = None
    created_at: str = ""

    def __post_init__(self):
        if not self.task_id:
            self.task_id = str(uuid.uuid4())[:8]
        if not self.created_at:
            self.created_at = datetime.now().isoformat()


@dataclass
class TimelineEvent:
    id: str
    timestamp: float
    agent: str
    event_type: EventType
    content: str
    thread_id: str = ""
    references: List[str] = field(default_factory=list)


@dataclass
class DiscussionRoom:
    id: str
    topic: str
    agents: List[str] = field(default_factory=list)
    events: List[TimelineEvent] = field(default_factory=lambda: [])
    status: str = "active"
    created_at: str = ""
    max_turns: int = 5
    current_turn: int = 0

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now().isoformat()


def build_speaker_queue(participants: List[str], roles: Optional[Dict[str, str]] = None) -> List[str]:
    if not roles:
        return participants.copy()
    role_index = {role: i for i, role in enumerate(ROLE_ORDER)}
    scored = [(role_index.get(roles.get(p, ""), 999), p) for p in participants]
    scored.sort(key=lambda x: x[0])
    return [p for _, p in scored]


def open_discussion_cycle(
    task: TaskCard, opened_by: str, participants: List[str],
    roles: Optional[Dict[str, str]] = None,
) -> TaskCard:
    cycle = DiscussionCycle(
        opened_by=opened_by,
        expected_participant_ids=build_speaker_queue(participants, roles),
    )
    task.stage = TaskStage.DISCUSSION
    task.discussion_cycle = cycle
    return task


def close_discussion_cycle(task: TaskCard) -> TaskCard:
    task.stage = TaskStage.EXECUTION
    return task


def mark_speaker_complete(task: TaskCard, participant_id: str) -> TaskCard:
    cycle = task.discussion_cycle
    if not cycle:
        return task
    if participant_id not in cycle.completed_participant_ids:
        cycle.completed_participant_ids.append(participant_id)
    return task


def next_speaker(task: TaskCard) -> Optional[str]:
    cycle = task.discussion_cycle
    if not cycle:
        return None
    for pid in cycle.expected_participant_ids:
        if pid not in cycle.completed_participant_ids:
            return pid
    return None


class CollaborationHall:
    """Manages multi-agent collaboration discussions"""

    def __init__(self):
        self._rooms: Dict[str, DiscussionRoom] = {}
        self._threads: Dict[str, BoundedHistory] = {}
        self._task_cards: Dict[str, TaskCard] = {}
        self._room_tasks: Dict[str, List[str]] = {}

    def create_room(self, topic: str, agents: List[str], room_id: str = None) -> DiscussionRoom:
        import uuid
        rid = room_id or str(uuid.uuid4())[:8]
        room = DiscussionRoom(id=rid, topic=topic, agents=agents)
        self._rooms[rid] = room
        logger.info(f"Collaboration room created: {rid} - {topic} (agents: {agents})")
        return room

    def add_event(self, room_id: str, agent: str, event_type: EventType, content: str, thread_id: str = "", references: List[str] = None) -> TimelineEvent:
        room = self._rooms.get(room_id)
        if not room:
            raise ValueError(f"Room not found: {room_id}")

        import uuid
        event = TimelineEvent(
            id=str(uuid.uuid4())[:8],
            timestamp=time.time(),
            agent=agent,
            event_type=event_type,
            content=content,
            thread_id=thread_id,
            references=references or [],
        )
        room.events.append(event)

        # Track threads
        if thread_id:
            if thread_id not in self._threads:
                self._threads[thread_id] = BoundedHistory(maxlen=500)
            self._threads[thread_id].append(event)

        # Increment turn on message
        if event_type == EventType.MESSAGE:
            room.current_turn += 1
            if room.current_turn >= room.max_turns:
                room.status = "converged"
                logger.info(f"Room {room_id} converged after {room.max_turns} turns")

        return event

    def handoff(self, room_id: str, from_agent: str, to_agent: str, context: str) -> TimelineEvent:
        return self.add_event(room_id, from_agent, EventType.HANDOFF,
                            f"Handoff to {to_agent}: {context}")

    def add_evidence(self, room_id: str, agent: str, evidence: str, thread_id: str = "") -> TimelineEvent:
        return self.add_event(room_id, agent, EventType.EVIDENCE, evidence, thread_id)

    def get_timeline(self, room_id: str) -> List[Dict]:
        room = self._rooms.get(room_id)
        if not room:
            return []
        return [
            {
                "id": e.id,
                "timestamp": datetime.fromtimestamp(e.timestamp).isoformat(),
                "agent": e.agent,
                "type": e.event_type.value,
                "content": e.content[:200],
                "thread_id": e.thread_id,
            }
            for e in room.events
        ]

    def get_thread(self, thread_id: str) -> List[Dict]:
        events = self._threads.get(thread_id, BoundedHistory(maxlen=500)).get_all()
        return [
            {
                "id": e.id,
                "agent": e.agent,
                "type": e.event_type.value,
                "content": e.content[:200],
            }
            for e in events
        ]

    def list_rooms(self) -> List[Dict]:
        return [
            {
                "id": r.id,
                "topic": r.topic,
                "agents": r.agents,
                "status": r.status,
                "events": len(r.events),
                "turns": f"{r.current_turn}/{r.max_turns}",
            }
            for r in self._rooms.values()
        ]

    def get_event_count(self, room_id: str) -> int:
        room = self._rooms.get(room_id)
        return len(room.events) if room else 0

    def create_task_card(self, room_id: str, title: str, description: str = "", assigned_to: str = "") -> TaskCard:
        task = TaskCard(title=title, description=description, assigned_to=assigned_to)
        self._task_cards[task.task_id] = task
        if room_id not in self._room_tasks:
            self._room_tasks[room_id] = []
        self._room_tasks[room_id].append(task.task_id)
        logger.info(f"Task card created: {task.task_id} - {title} (room: {room_id})")
        return task

    def get_task_card(self, task_id: str) -> Optional[TaskCard]:
        return self._task_cards.get(task_id)

    def update_task_card(self, task_id: str, **kwargs) -> Optional[TaskCard]:
        task = self._task_cards.get(task_id)
        if not task:
            return None
        for key, value in kwargs.items():
            if hasattr(task, key):
                setattr(task, key, value)
        return task

    def get_room_tasks(self, room_id: str) -> List[TaskCard]:
        task_ids = self._room_tasks.get(room_id, [])
        return [self._task_cards[tid] for tid in task_ids if tid in self._task_cards]

    def transfer_ownership(self, task_id: str, new_owner: str) -> Optional[TaskCard]:
        task = self._task_cards.get(task_id)
        if not task:
            return None
        task.assigned_to = new_owner
        return task

    def get_stats(self) -> Dict:
        total_events = sum(len(r.events) for r in self._rooms.values())
        total_tasks = len(self._task_cards)
        tasks_by_stage = {}
        for t in self._task_cards.values():
            tasks_by_stage[t.stage.value] = tasks_by_stage.get(t.stage.value, 0) + 1
        return {
            "total_rooms": len(self._rooms),
            "active_rooms": sum(1 for r in self._rooms.values() if r.status == "active"),
            "converged_rooms": sum(1 for r in self._rooms.values() if r.status == "converged"),
            "total_events": total_events,
            "total_threads": len(self._threads),
            "total_tasks": total_tasks,
            "tasks_by_stage": tasks_by_stage,
        }
