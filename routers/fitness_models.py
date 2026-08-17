"""OpenAPI response models for Fitness workout routes."""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from shared.fitness import store


class PlannedExerciseOut(BaseModel):
    id: str
    day_id: str
    name: str
    target_sets: Optional[int] = None
    target_reps: Optional[int] = None
    rest_seconds: Optional[int] = None
    sort_order: int
    notes: Optional[str] = None


class PlanDayOut(BaseModel):
    id: str
    plan_id: str
    sort_order: int
    title: Optional[str] = None
    exercises: list[PlannedExerciseOut] = Field(default_factory=list)


class WorkoutPlanOut(BaseModel):
    id: str
    title: Optional[str] = None
    notes: Optional[str] = None
    source_upload_ref: Optional[str] = None
    is_active: bool
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    days: list[PlanDayOut] = Field(default_factory=list)


class PlanSummaryOut(BaseModel):
    id: str
    title: Optional[str] = None
    notes: Optional[str] = None
    source_upload_ref: Optional[str] = None
    is_active: bool
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class PlanListOut(BaseModel):
    plans: list[PlanSummaryOut]


class CurrentExerciseOut(BaseModel):
    id: str
    name: str
    target_sets: Optional[int] = None
    target_reps: Optional[int] = None
    rest_seconds: Optional[int] = None
    notes: Optional[str] = None


class WorkoutSessionStateOut(BaseModel):
    session_id: str
    status: Literal["active", "completed", "abandoned"]
    session_date: Optional[str] = None
    plan_day_id: Optional[str] = None
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    current_exercise: Optional[CurrentExerciseOut] = None
    current_set_number: int
    rest_seconds: Optional[int] = None
    sets_logged: int
    remaining_sets_on_current: int


class StartSessionOut(WorkoutSessionStateOut):
    resumed: bool


class SetLogOut(BaseModel):
    id: str
    session_id: str
    exercise_id: str
    set_number: int
    reps: Optional[int] = None
    weight: Optional[float] = None
    source: str
    completed_at: Optional[str] = None
    exercise_name: Optional[str] = None


class VisualPanelOut(BaseModel):
    type: str
    data: dict[str, Any] = Field(default_factory=dict)


class LogSetOut(BaseModel):
    set: SetLogOut
    is_new_pr: bool
    pr_announcement: Optional[str] = None
    reply: str
    pending_action: None = None
    visual_panel: Optional[VisualPanelOut] = None
    state: WorkoutSessionStateOut


class SessionDetailOut(WorkoutSessionStateOut):
    sets: list[SetLogOut] = Field(default_factory=list)
    exercises: list[PlannedExerciseOut] = Field(default_factory=list)


class SessionSummaryOut(BaseModel):
    id: str
    session_date: Optional[str] = None
    plan_day_id: Optional[str] = None
    status: Literal["active", "completed", "abandoned"]
    started_at: Optional[str] = None
    ended_at: Optional[str] = None


class HistoryOut(BaseModel):
    sessions: list[SessionSummaryOut]
    limit: int = Field(..., ge=1, le=store.MAX_HISTORY_LIMIT)


class ExerciseHistoryOut(BaseModel):
    exercise: PlannedExerciseOut
    sets: list[SetLogOut]
    weight_unit: None = None
    weight_unit_note: str


class PersonalRecordOut(BaseModel):
    id: str
    exercise_id: str
    rep_range: int
    weight: float
    achieved_at: Optional[str] = None
    exercise_name: Optional[str] = None


class PersonalRecordsOut(BaseModel):
    personal_records: list[PersonalRecordOut]
    weight_unit: None = None
    weight_unit_note: str


class RecentFrequencyOut(BaseModel):
    window_days: int
    sessions_completed: int


class AdherenceOut(BaseModel):
    sessions_completed: int
    sessions_abandoned: int
    sets_completed: int
    last_workout: Optional[SessionSummaryOut] = None
    recent_frequency: RecentFrequencyOut


class VoiceLogOut(BaseModel):
    sets: list[SetLogOut]
    pr_announcements: list[str]
    reply: str
    visual_panel: Optional[VisualPanelOut] = None
    pending_action: None = None
    state: Optional[WorkoutSessionStateOut] = None
