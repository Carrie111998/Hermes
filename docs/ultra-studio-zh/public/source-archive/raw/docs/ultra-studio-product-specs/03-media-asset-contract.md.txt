# Media And Asset Contract

Status: product/backend contract  
Date: 2026-06-10

## Goal

Make every upload, generated image, generated video, character, and element a
typed product object. The agent should pass structured asset refs, not paste raw
ids into prompt text.

## Asset Types

| Type | Meaning |
|---|---|
| `media_input` | User-uploaded image/video/audio/file. |
| `image_job` | Generated image output. |
| `video_job` | Generated video output. |
| `audio_job` | Generated audio output. |
| `element` | Reusable visual element such as product, logo, prop, scene. |
| `character` | Reusable person/creature/avatar identity. |
| `soul_id` | Provider or platform identity reference for consistency. |
| `task_file` | Session file not yet promoted to product asset. |

## Media Job Envelope

Provider APIs should not be exposed raw to the agent. Use a provider-neutral job
envelope.

```yaml
MediaJob:
  job_id:
  session_id:
  run_id:
  tool_call_id:
  provider:
  model:
  media_type:
  mode:
  status:
  input_assets:
  prompt:
  negative_prompt:
  provider_constraints:
  seed:
  tokenrouter_decision_id:
  output_assets:
  error:
```

## Required Job Tools

| Tool | Purpose |
|---|---|
| `ultra_media_job_create` | Create image/video/audio job with structured inputs. |
| `ultra_media_job_status` | Return durable job state and progress. |
| `ultra_media_job_cancel` | Cancel queued/running job if supported. |
| `ultra_media_job_retry` | Retry with compiled repair plan. |
| `ultra_media_job_finalize` | Register outputs as assets, thumbnails, lineage. |
| `ultra_media_constraints_get` | Return model/provider limits before prompt compile. |

## Asset Lifecycle

```text
uploading
  -> processing
  -> ready
  -> archived
```

Failure states:

- `failed`
- `revoked`
- `deleted`

Generated outputs:

```text
job.created
  -> job.running
  -> job.succeeded
  -> asset.processing
  -> asset.ready
```

## Asset Card UI

Every media card should expose:

- preview
- status
- media type
- provider/model
- dimensions/duration
- prompt hash
- input asset refs
- job id
- download
- inspect
- reuse
- convert to element
- create character, when eligible

The card should not expose internal filesystem paths by default.

## Lineage

Lineage must capture:

- parent asset ids
- source job id
- provider job id
- model and endpoint
- prompt hash
- seed/params
- user/session/run
- output asset ids

This is the basis for "use this again", "make a variation", "why did this fail",
and "where did this asset come from".

## QA

QA must separate observed facts from inferred quality.

Observed:

- media can be downloaded
- file exists
- duration/dimensions
- first frame/thumbnail available
- job succeeded

Inferred:

- prompt alignment
- style fit
- character consistency
- readability
- visual defects

The agent cannot claim visual quality without an observation step or user review.

## Acceptance

- Uploads and generated media produce asset ids.
- All generation results have lineage.
- The inspector can open any asset card and show model/job/prompt/input details.
- Download action uses real storage/object URLs or local materialization.
- Failed jobs remain inspectable.

