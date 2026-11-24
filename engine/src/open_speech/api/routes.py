"""REST routers (04). Thin adapters: parse, call `service/`, return the schema. No behavior lives here."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Literal

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response

from open_speech.api import schemas as s
from open_speech.config import UserSettings

if TYPE_CHECKING:
    from open_speech.service.engine import Engine


def get_engine(request: Request) -> Engine:
    return request.app.state.engine  # type: ignore[no-any-return]


E = Annotated["Engine", Depends(get_engine)]

router = APIRouter(prefix="/api/v1")

# ------------------------------------------------------------------ documents


@router.post("/documents", response_model=s.CreateDocumentResponse, tags=["documents"])
async def create_document(body: s.CreateDocumentRequest, engine: E) -> s.CreateDocumentResponse:
    return await engine.documents.create(body)


@router.post("/documents/upload", response_model=s.CreateDocumentResponse, tags=["documents"])
async def upload_document(
    engine: E,
    file: Annotated[UploadFile, File()],
    play: Annotated[bool, Form()] = False,
    voice_id: Annotated[str | None, Form()] = None,
    confirm_estimate: Annotated[bool, Form()] = False,
) -> s.CreateDocumentResponse:
    data = await file.read()
    return await engine.documents.create_from_upload(
        file.filename or "upload", data, play=play, voice_id=voice_id, confirm_estimate=confirm_estimate
    )


@router.get("/documents", response_model=s.DocumentList, tags=["documents"])
async def list_documents(
    engine: E, q: str | None = None, limit: Annotated[int, Query(ge=1, le=200)] = 10
) -> s.DocumentList:
    return await engine.documents.list(q=q, limit=limit)


@router.get("/documents/{document_id}", response_model=s.DocumentOut, tags=["documents"])
async def get_document(document_id: str, engine: E) -> s.DocumentOut:
    return await engine.documents.get(document_id)


@router.get("/documents/{document_id}/blocks", response_model=s.BlocksPage, tags=["documents"])
async def get_blocks(
    document_id: str,
    engine: E,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=5000)] = 500,
) -> s.BlocksPage:
    return await engine.documents.blocks(document_id, offset=offset, limit=limit)


@router.get("/documents/{document_id}/script", response_model=s.ScriptOut, tags=["documents"])
async def get_script(document_id: str, engine: E) -> s.ScriptOut:
    return await engine.documents.script(document_id)


@router.get("/documents/{document_id}/source", tags=["documents"], response_class=FileResponse)
async def get_source(document_id: str, engine: E) -> FileResponse:
    path, media_type = await engine.documents.source_file(document_id)
    return FileResponse(path, media_type=media_type)


@router.post("/documents/{document_id}/regenerate", response_model=s.ScriptOut, tags=["documents"])
async def regenerate(document_id: str, engine: E, confirm_estimate: bool = False) -> s.ScriptOut:
    return await engine.documents.regenerate(document_id, confirm_estimate=confirm_estimate)


@router.put("/documents/{document_id}/settings", response_model=s.DocumentOut, tags=["documents"])
async def document_settings(document_id: str, body: s.DocumentSettingsUpdate, engine: E) -> s.DocumentOut:
    return await engine.documents.update_settings(document_id, body)


@router.delete("/documents/{document_id}", status_code=204, tags=["documents"])
async def delete_document(document_id: str, engine: E) -> Response:
    await engine.documents.delete(document_id)
    return Response(status_code=204)


# ------------------------------------------------------------------ playback


@router.get("/playback", response_model=s.PlaybackState, tags=["playback"])
async def playback_state(engine: E) -> s.PlaybackState:
    return engine.playback.state()


@router.post("/playback/play", response_model=s.PlaybackState, tags=["playback"])
async def play(body: s.PlayRequest, engine: E) -> s.PlaybackState:
    return await engine.playback.play(body)


@router.post("/playback/pause", response_model=s.PlaybackState, tags=["playback"])
async def pause(engine: E) -> s.PlaybackState:
    return await engine.playback.pause()


@router.post("/playback/resume", response_model=s.PlaybackState, tags=["playback"])
async def resume(engine: E) -> s.PlaybackState:
    return await engine.playback.resume()


@router.post("/playback/stop", response_model=s.PlaybackState, tags=["playback"])
async def stop(engine: E) -> s.PlaybackState:
    return await engine.playback.stop()


@router.post("/playback/skip", response_model=s.PlaybackState, tags=["playback"])
async def skip(body: s.SkipRequest, engine: E) -> s.PlaybackState:
    return await engine.playback.skip(body)


@router.post("/playback/queue", response_model=s.PlaybackState, tags=["playback"])
async def enqueue(body: s.QueueRequest, engine: E) -> s.PlaybackState:
    """Read several documents one after another."""
    return await engine.playback.enqueue(body)


@router.delete("/playback/queue", response_model=s.PlaybackState, tags=["playback"])
async def clear_queue(engine: E) -> s.PlaybackState:
    return await engine.playback.clear_queue()


@router.post("/playback/next", response_model=s.PlaybackState, tags=["playback"])
async def next_document(engine: E) -> s.PlaybackState:
    """Leave the current document and start the next one in the queue."""
    return await engine.playback.next_document()


@router.put("/playback/speed", response_model=s.PlaybackState, tags=["playback"])
async def speed(body: s.SpeedRequest, engine: E) -> s.PlaybackState:
    return await engine.playback.set_speed(body.speed)


@router.put("/playback/voice", response_model=s.PlaybackState, tags=["playback"])
async def voice(body: s.VoiceRequest, engine: E) -> s.PlaybackState:
    return await engine.playback.set_voice(body)


# ------------------------------------------------------------------ voices


@router.get("/voices", response_model=s.VoiceList, tags=["voices"])
async def list_voices(engine: E, provider: str | None = None, lang: str | None = None) -> s.VoiceList:
    return engine.voices.list(provider=provider, lang=lang)


@router.get("/voices/clones", response_model=s.CloneList, tags=["voices"])
async def list_clones(engine: E) -> s.CloneList:
    """Cloned and designed voices (kept ones)."""
    return engine.clones.listing()


@router.get("/voices/clones/passages", response_model=s.PassageList, tags=["voices"])
async def clone_passages(engine: E, language: str | None = None) -> s.PassageList:
    """Passages to read for a clone (two sentences, ~8-10 s), and the consent line to show with them."""
    return engine.clones.passages(language)


@router.post("/voices/clones", response_model=s.CloneAccepted, status_code=202, tags=["voices"])
async def create_clone(
    engine: E,
    file: Annotated[UploadFile, File()],
    name: Annotated[str | None, Form(max_length=64)] = None,
    consent: Annotated[bool, Form()] = False,
    language: Annotated[str | None, Form(max_length=8)] = None,
    ref_text: Annotated[str | None, Form(max_length=2000)] = None,
    passage_id: Annotated[str | None, Form(max_length=16)] = None,
    source: Annotated[Literal["recording", "file"], Form()] = "file",
    keep: Annotated[bool, Form()] = True,
    session_id: Annotated[str | None, Form(max_length=64)] = None,
    conversation_id: Annotated[str | None, Form(max_length=64)] = None,
) -> s.CloneAccepted:
    """Make a voice from a recording or an audio file. `consent=true` is required (422 otherwise): the person
    confirms it is their voice or they have the speaker's permission. With `passage_id` the recording must say that
    passage. `keep=false` leaves it unlisted until kept (the bar's "Keep it?"); `session_id` answers in that
    conversation with the check sentence in the new voice."""
    data = await file.read()
    return await engine.clones.create(
        data=data, filename=file.filename or "recording.wav", name=name, consent=consent, language=language,
        ref_text=ref_text, passage_id=passage_id, source=source, keep=keep, session_id=session_id,
        conversation_id=conversation_id,
    )


@router.post("/voices/clones/design", response_model=s.DesignAccepted, status_code=202, tags=["voices"])
async def design_voices(body: s.DesignRequest, engine: E) -> s.DesignAccepted:
    """Three candidate voices from a description; keep one with POST /voices/clones/design/{candidate_id}."""
    return await engine.clones.design(body.description, name=body.name, language=body.language)


@router.post("/voices/clones/design/{candidate_id}", response_model=s.CloneOut, tags=["voices"])
async def keep_designed(candidate_id: str, engine: E, body: s.KeepCandidate | None = None) -> s.CloneOut:
    """Keep a designed candidate as a voice (`source = "design"`)."""
    return await engine.clones.keep_candidate(candidate_id, name=body.name if body else None)


@router.patch("/voices/clones/{slug}", response_model=s.CloneOut, tags=["voices"])
async def patch_clone(slug: str, body: s.ClonePatch, engine: E) -> s.CloneOut:
    return await engine.clones.patch(slug, body)


@router.post("/voices/clones/{slug}/reference", response_model=s.CloneAccepted, status_code=202, tags=["voices"])
async def replace_reference(
    slug: str,
    engine: E,
    file: Annotated[UploadFile, File()],
    consent: Annotated[bool, Form()] = False,
    ref_text: Annotated[str | None, Form(max_length=2000)] = None,
    passage_id: Annotated[str | None, Form(max_length=16)] = None,
    source: Annotated[Literal["recording", "file"], Form()] = "file",
) -> s.CloneAccepted:
    """Replace a voice's recording (same id and name; its cached audio is made again)."""
    data = await file.read()
    return await engine.clones.create(
        data=data, filename=file.filename or "recording.wav", consent=consent, ref_text=ref_text,
        passage_id=passage_id, source=source, keep=True, replace=slug,
    )


@router.delete("/voices/clones/{slug}", status_code=204, tags=["voices"])
async def delete_clone(slug: str, engine: E) -> Response:
    """Delete a voice: its folder, its cached audio; settings that used it go back to "auto"."""
    await engine.clones.delete(slug)
    return Response(status_code=204)


@router.post("/voices/{voice_id}/preview", response_model=s.PreviewResponse, tags=["voices"])
async def preview_voice(
    voice_id: str,
    engine: E,
    style: str | None = None,
    play: bool = True,
    text: Annotated[str | None, Query(max_length=500)] = None,
) -> s.PreviewResponse:
    """A short sample (`text`, or the voice introducing itself). `play=false` only makes the recording."""
    return await engine.voices.preview(voice_id, style=style, play=play, text=text)


@router.get("/jobs/{job_id}", response_model=s.JobOut, tags=["system"])
async def get_job(job_id: str, engine: E) -> s.JobOut:
    """A background job: a clone or designed voices being made, or a model download."""
    job = engine.jobs.get(job_id)
    if job is not None:
        return s.JobOut.model_validate(job.as_dict())
    m = engine.model_manager.job(job_id)
    if m is None:
        from open_speech.errors import NotFound

        raise NotFound("that job")
    state = {"running": "running", "done": "done"}.get(m["state"], "failed")
    return s.JobOut(job_id=job_id, kind="install", pct=m["pct"], label=m["label"], state=state,  # type: ignore[arg-type]
                    error={"detail": m["error"]} if m.get("error") else None, result={"model": m["model"]})


# ------------------------------------------------------------------ rules


@router.get("/rules", response_model=s.RuleList, tags=["rules"])
async def list_rules(engine: E, document_id: str | None = None) -> s.RuleList:
    return engine.rules.list(document_id=document_id)


@router.post("/rules/proposals", response_model=s.RuleProposal, tags=["rules"])
async def propose_rule(body: s.RuleProposalRequest, engine: E) -> s.RuleProposal:
    return await engine.rules.propose(body)


@router.post("/rules", response_model=s.RuleOut, tags=["rules"])
async def save_rule(body: s.SaveRuleRequest, engine: E) -> s.RuleOut:
    return await engine.rules.save(body)


@router.get("/rules/lexicon", response_model=s.LexiconList, tags=["rules"])
async def get_lexicon(engine: E) -> s.LexiconList:
    return engine.rules.lexicon()


@router.post("/rules/lexicon", response_model=s.LexiconList, tags=["rules"])
async def add_lexicon(body: s.LexiconEntry, engine: E) -> s.LexiconList:
    return await engine.rules.add_lexicon(body)


@router.delete("/rules/lexicon/{term}", response_model=s.LexiconList, tags=["rules"])
async def delete_lexicon(term: str, engine: E) -> s.LexiconList:
    return await engine.rules.delete_lexicon(term)


@router.get("/rules/files", response_model=s.RuleFileList, tags=["rules"])
async def rule_files(engine: E) -> s.RuleFileList:
    """The text files behind the rules: standing preferences, pronunciations, and one file per rule."""
    return engine.rules.files()


@router.post("/rules/files", response_model=s.RuleFileContent, tags=["rules"])
async def create_rule_file(body: s.RuleFileWrite, engine: E) -> s.RuleFileContent:
    return await engine.rules.write_file(None, body)


@router.get("/rules/files/{file_id}", response_model=s.RuleFileContent, tags=["rules"])
async def read_rule_file(file_id: str, engine: E) -> s.RuleFileContent:
    return engine.rules.read_file(file_id)


@router.put("/rules/files/{file_id}", response_model=s.RuleFileContent, tags=["rules"])
async def write_rule_file(file_id: str, body: s.RuleFileWrite, engine: E) -> s.RuleFileContent:
    return await engine.rules.write_file(file_id, body)


@router.patch("/rules/{rule_id}", response_model=s.RuleOut, tags=["rules"])
async def patch_rule(rule_id: str, body: s.RulePatch, engine: E) -> s.RuleOut:
    return await engine.rules.patch(rule_id, body)


@router.delete("/rules/{rule_id}", status_code=204, tags=["rules"])
async def delete_rule(rule_id: str, engine: E) -> Response:
    await engine.rules.delete(rule_id)
    return Response(status_code=204)


@router.get("/skills", tags=["rules"])
async def list_skills(engine: E) -> dict[str, Any]:
    return {"items": [], "note": "Skills are P1."}


# ------------------------------------------------------------------ assistant


@router.post("/assistant/turns", response_model=s.TurnAccepted, status_code=202, tags=["assistant"])
async def assistant_turn(body: s.TurnRequest, engine: E) -> s.TurnAccepted:
    return await engine.assistant.submit(body)


@router.post("/assistant/say", status_code=204, tags=["assistant"])
async def assistant_say(body: s.SayRequest, engine: E) -> None:
    await engine.assistant.say(body.text)


@router.post("/assistant/speech/stop", status_code=204, tags=["assistant"])
async def assistant_speech_stop(body: s.SpeechStopRequest, engine: E) -> None:
    await engine.stop_speech(body.turn_id)


@router.get("/actions", response_model=list[s.ActionOut], tags=["assistant"])
async def pending_actions(engine: E) -> list[s.ActionOut]:
    return engine.assistant.pending_actions()


@router.post("/actions/{action_id}/confirm", response_model=s.ActionResult, tags=["assistant"])
async def confirm_action(action_id: str, engine: E) -> s.ActionResult:
    return await engine.assistant.confirm(action_id)


@router.post("/actions/{action_id}/cancel", response_model=s.ActionResult, tags=["assistant"])
async def cancel_action(action_id: str, engine: E) -> s.ActionResult:
    return await engine.assistant.cancel(action_id)


# ------------------------------------------------------------------ usage


@router.get("/usage/summary", response_model=s.UsageSummary, tags=["usage"])
async def usage_summary(
    engine: E,
    period: Literal["today", "week", "month"] = "month",
    group_by: Literal["provider", "op", "document"] = "provider",
) -> s.UsageSummary:
    return engine.usage.summary(period=period, group_by=group_by)


@router.get("/usage/ledger", response_model=s.LedgerPage, tags=["usage"])
async def usage_ledger(
    engine: E,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    provider: str | None = None,
    op: str | None = None,
    document_id: str | None = None,
) -> s.LedgerPage:
    return engine.usage.ledger(offset=offset, limit=limit, provider=provider, op=op, document_id=document_id)


@router.post("/usage/estimate", response_model=s.Estimate, tags=["usage"])
async def usage_estimate(body: s.EstimateRequest, engine: E) -> s.Estimate:
    return await engine.usage.estimate(body)


@router.get("/usage/budget", response_model=s.BudgetState, tags=["usage"])
async def get_budget(engine: E) -> s.BudgetState:
    return engine.usage.budget()


@router.put("/usage/budget", response_model=s.BudgetState, tags=["usage"])
async def put_budget(body: s.BudgetUpdate, engine: E) -> s.BudgetState:
    return await engine.usage.set_budget(body)


# ------------------------------------------------------------------ settings, capabilities, models, recordings


@router.get("/settings", response_model=UserSettings, tags=["settings"])
async def get_settings(engine: E) -> UserSettings:
    return engine.settings.get()


@router.patch("/settings", response_model=UserSettings, tags=["settings"])
async def patch_settings(body: dict[str, Any], engine: E) -> UserSettings:
    """JSON merge patch over `settings.toml` (nested objects merge; scalars replace)."""
    return await engine.settings.patch(body)


@router.get("/capabilities", response_model=s.Capabilities, tags=["system"])
async def capabilities(engine: E) -> s.Capabilities:
    """What this build can do. Provider keys are build configuration, so this is all a client ever learns
    about them: which features exist."""
    return engine.capabilities()


@router.get("/models/local", response_model=s.LocalModelList, tags=["settings"])
async def local_models(engine: E, all: bool = False) -> s.LocalModelList:  # noqa: A002 — the query parameter
    """Local models. Of the downloadable weight variants (Qwen3-TTS sizes x precisions) only the installed ones, the
    ones downloading and the ones the settings choose are listed, unless `all=1`."""
    return engine.models.list(all=all)


@router.post("/models/local/{model_id}", response_model=s.JobAccepted, status_code=202, tags=["settings"])
async def install_model(model_id: str, engine: E) -> s.JobAccepted:
    return await engine.models.install(model_id)


@router.delete("/models/local/{model_id}", status_code=204, tags=["settings"])
async def remove_model(model_id: str, engine: E) -> Response:
    await engine.models.remove(model_id)
    return Response(status_code=204)


@router.get("/recordings/{recording_id}", tags=["assistant"], response_class=FileResponse)
async def get_recording(recording_id: str, engine: E) -> FileResponse:
    path = engine.recordings.path(recording_id)
    return FileResponse(path, media_type="audio/ogg")


@router.post("/recordings/{recording_id}/play", response_model=s.PlaybackState, tags=["assistant"])
async def play_recording(recording_id: str, engine: E) -> s.PlaybackState:
    """Replay a recording through the engine's output (FR-BAR-6) — reading ducks around it."""
    return await engine.recordings.play(recording_id)


@router.get("/diagnostics/audio", response_model=s.AudioDiagnostics, tags=["system"])
async def audio_diagnostics(engine: E) -> s.AudioDiagnostics:
    """Underflows, starvations, ring low-water and callback timing since the engine started (05 § 9)."""
    return s.AudioDiagnostics.model_validate(engine.transport.audio_diagnostics())


@router.get("/health", response_model=s.Health, tags=["system"])
async def health(engine: E) -> s.Health:
    return engine.health()
