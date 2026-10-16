from typing import Any, Literal
from pydantic import BaseModel, Field, model_validator


class RecordingJobRequest(BaseModel):
    asset_id: str
    language: str = "hi-en"
    technical_vocabulary: list[str] = Field(default_factory=list)
    target_clip_seconds: int = Field(default=45, ge=30, le=60)
    max_clips: int = Field(default=10, ge=1, le=50)
    brand_id: str | None = None


class ExplainerJobRequest(BaseModel):
    topic: str = Field(min_length=2, max_length=300)
    audience: str = "beginner"
    duration_seconds: int = Field(default=45, ge=30, le=60)
    language: str = "en"
    brand_id: str | None = None


class BrandKit(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    colors: dict[str, str] = Field(default_factory=dict)
    fonts: dict[str, str] = Field(default_factory=dict)
    logo_asset_id: str | None = None
    intro_asset_id: str | None = None
    outro_asset_id: str | None = None
    caption_style: dict[str, Any] = Field(default_factory=dict)


class ClipEdit(BaseModel):
    title: str | None = None
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0)

    @model_validator(mode="after")
    def valid_range(self):
        if self.end_seconds <= self.start_seconds:
            raise ValueError("end_seconds must be greater than start_seconds")
        return self


class RenderRequest(BaseModel):
    aspect_ratio: Literal["9:16"] = "9:16"
    resolution: Literal["1080x1920", "720x1280"] = "1080x1920"
    brand_id: str | None = None
    caption_style: dict[str, Any] = Field(default_factory=dict)


class TranscriptEdit(BaseModel):
    transcript_text: str = Field(max_length=2_000_000)
