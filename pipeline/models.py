"""
models.py - validation schemas for every OpenF1 table we load.

Every record coming from the API is pushed through one of these models.
A record that fails is NOT silently dropped: etl.py stores it in the
`rejected` table together with ALL the errors and the complete raw record,
so the dashboard can show exactly what was thrown away and why.

The numeric limits below are deliberately wide "sanity" limits (is this
physically plausible?). Statistical cleaning (slow laps, out-laps, ...) is
done later in eda.py and is configured in config.py.
"""
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class Lap(BaseModel):
    session_key: int
    driver_number: int
    lap_number: int = Field(ge=1, le=100)
    lap_duration: Optional[float] = Field(default=None, gt=40, lt=300)  # seconds; None = no time set
    is_pit_out_lap: Optional[bool] = False
    date_start: Optional[str] = None
    duration_sector_1: Optional[float] = None
    duration_sector_2: Optional[float] = None
    duration_sector_3: Optional[float] = None
    i1_speed: Optional[float] = None
    i2_speed: Optional[float] = None
    st_speed: Optional[float] = None

    @field_validator("is_pit_out_lap", mode="before")
    @classmethod
    def _none_is_false(cls, v):
        """The API sends null for 'not an out-lap'; treat that as False."""
        return False if v is None else v


class Stint(BaseModel):
    session_key: int
    driver_number: int
    stint_number: int = Field(ge=1)
    compound: str
    lap_start: int = Field(ge=1)
    lap_end: int = Field(ge=1)
    tyre_age_at_start: Optional[int] = 0


class Pit(BaseModel):
    session_key: int
    driver_number: int
    lap_number: int = Field(ge=1)
    pit_duration: float = Field(gt=1, lt=120)   # seconds in the pit lane


class Weather(BaseModel):
    session_key: int
    date: str
    air_temperature: float = Field(gt=-10, lt=70)
    track_temperature: float = Field(gt=-10, lt=90)
    humidity: Optional[float] = None
    rainfall: Optional[int] = 0


class Position(BaseModel):
    session_key: int
    driver_number: int
    date: str
    position: int = Field(ge=1, le=25)


class RaceControl(BaseModel):
    session_key: int
    date: str
    category: Optional[str] = None
    flag: Optional[str] = None
    message: Optional[str] = None
    lap_number: Optional[int] = None


class Location(BaseModel):
    session_key: int
    driver_number: int
    date: str
    x: float
    y: float
    z: float
