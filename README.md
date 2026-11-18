# cutdown

Two modes over the same interview.

**`cutdown run` / `studio`** cuts a diarized interview into track-anchored radio
segments: 1–4 minutes of the guest talking about a record, placed before that
record plays. It does **assignment and ordering of existing speech**. Nothing is
generated, nothing is summarised, nothing is rewritten. Every output sample is a
byte copy from the master WAV.

**`cutdown notes`** goes the other way. You have already made the edit in the
DAW and exported the clips; it reads them back and writes the document you send
to someone — a verbatim transcript per clip, with the anecdotes in it summarised
and timestamped. See [Notes mode](#notes-mode).

For the complete user-facing flow, model routes, decision points, diagrams, and
output guide, see [PROCESS.md](PROCESS.md).

## Quick start

```sh
cutdown
```

That is the whole job. It asks for the recording (drag the file into the
terminal), then shows the context screen with your last preset already picked.
Type today's detail, press **Start**, and every stage runs: transcribe, ingest,
discover, label, assemble, topics, check, then asks `Revise? [y/N]` when there are
warnings, before render. Run `cutdown` again on the same
file and it picks up where it left off. `cutdown today.wav --preset radio` skips
the questions it can answer from the command line.

Each recording gets its own folder in `ep/<audio name>/` under the working directory:

```
ep/<audio name>/
  1-final/     seg01_belinda-says.wav     one finished file per song
               topic01_touring.wav        then one per topic of leftover chat
  2-clips/     seg01_belinda-says/        the BWF clips behind each final file
               topic01_touring/
  3-chunks/    C01_00m13s_production-talk.wav     the recording, unedited,
               C02_04m51s_belinda-says.wav        cut at its silences, named
                                                   for what is in each
  report.md  manifest.csv  rejected.csv  brief.yaml
  build/  runs/        build includes roles.json and pre-revise/
```

## The load-bearing rules

These are enforced in code, not requested in a prompt.

| Rule | Where |
|---|---|
| Models never emit timestamps — they get ID-tagged text and return IDs | `prompts/`, every stage schema |
| Code owns duration: it measures, and re-prompts with the measured figure | `assemble.py:measure` |
| Exclusivity: a unit assigned to a segment leaves the pool | `assemble.py`, asserted again in `render.py:check_exclusivity` |
| Fillers are removed by code from measured word gaps, never by a model | `spans.py` |
| A revision is all-or-nothing and keeps the originals | `revise.py`, `build/pre-revise/` |
| Cuts land in silence, from measured inter-word gaps | `ingest.py:cut_points`, `spans.py` |
| Every stage resumable, individually invocable | `state.py`, `pipeline.py` |
| No `admin`-tagged unit reaches a clip | `render.py:check_no_admin` |

## Install on macOS

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) once (`brew install uv` with Homebrew). From the checkout, build and install Cutdown into its own environment with one command.

```sh
./install.sh
```

The installer uses Python 3.12 and the checked-in lockfile. Run it again after updating the checkout. Run `./bin/cutdown` to launch Studio, or put the launcher on your `PATH` for the `cutdown` command used below.

```sh
mkdir -p "$HOME/.local/bin"
ln -s "$PWD/bin/cutdown" "$HOME/.local/bin/cutdown"
export PATH="$HOME/.local/bin:$PATH"
```

Set credentials for the stages you plan to use. Run these once from the checkout and edit `.env`.

```sh
install -m 600 .env.example .env
./bin/cutdown init
```

- `OPENROUTER_KEY` enables the default editorial and summary stages.
- `HF_TOKEN` enables mixed-channel speaker diarization. Accept access to [the diarization model](https://huggingface.co/pyannote/speaker-diarization-community-1) before using a read token. WhisperX and model weights are downloaded on first transcription. An existing WhisperX JSON works without downloading the transcription stack.
- `OPENCODE_KEY` is needed only if you configure a stage to use OpenCode.

Cutdown looks for `.env` from the current working directory up and gives exported credentials precedence. It prefers a working system FFmpeg and includes a bundled fallback. To verify the install, run `./bin/cutdown --help`.

## Self-host on Linux

Cutdown runs as a batch CLI over SSH or through Docker Compose. Use a dedicated working directory for trusted audio, transcripts and settings. The container runs as your numeric user with a read-only root filesystem and publishes no ports.

```sh
mkdir -p -m 700 work
install -m 600 .env.example work/.env
export CUTDOWN_UID=$(id -u) CUTDOWN_GID=$(id -g)
docker compose build --pull
docker compose run --rm -T cutdown init
```

Edit `work/.env` and put the master `interview.wav` and an existing WhisperX
`interview.json` in `work/`, then run:

```sh
docker compose run --rm -T cutdown run --json interview.json --master interview.wav -o ep/interview --no-revise
```

Results appear in `work/ep/interview/`. For optional local transcription,
replace `--json interview.json --master interview.wav` with `--audio interview.wav`.
The first transcription downloads dependencies and model weights into the
persistent cache under `work/.cache/`. Review the
[transcription dependency findings](SECURITY.md#optional-transcription-dependency)
before enabling that path on a server.

For a native server install with uv, run `./install.sh` and use `./bin/cutdown run` with the same arguments. Use [SECURITY.md](SECURITY.md) to review what is stored locally and what is sent to a provider.

## The interface

```sh
./fp.sh         # prompts for the audio-file path, then opens Studio
./fp.sh radio   # the same, starting from the `radio` preset
# Or supply the paths yourself:
cutdown studio --audio audio/interview.wav             # outdir ep/interview
cutdown studio -o ep/interview --audio audio/interview.wav --master audio/interview.wav
```

Stage status down the left, segments top right, the assembled text under it with
check warnings against the units they belong to, and the log at the bottom.

| key | |
|---|---|
| `v` | run the revision pass |
| `r` | run everything that is not current |
| `c` | context: load a preset, add today's detail, save as a preset |
| `enter` | run the highlighted stage |
| `f` | force the highlighted stage to re-run |
| `space` / `s` | play the selected song or topic file / stop |
| `e` | edit the brief in `$EDITOR` |
| `o` | open the output folder |
| `d` | toggle dry-run (prompts written, nothing sent) |

A new episode opens on the context screen.

## Or one stage at a time

```sh
cutdown transcribe audio/interview.wav -o ep/interview/build/transcript
# Or omit the file to be prompted for its path:
cutdown transcribe -o ep/interview/build/transcript
cutdown ingest     out/interview.json -o ep/interview/build --guest "Alex Vale"
cutdown discover   ep/interview/build -o brief.yaml         # draft run sheet, then correct it
cutdown label      ep/interview/build --brief brief.yaml
cutdown assemble   ep/interview/build --brief brief.yaml
cutdown topics     ep/interview/build --brief brief.yaml   # leftover talk -> topics
cutdown check      ep/interview/build
cutdown revise     ep/interview/build --brief brief.yaml
cutdown render     ep/interview/build --master audio/interview.wav -o ep/interview

cutdown run --json out/interview.json --master audio/interview.wav -o ep/interview   # brief drafted
cutdown run --brief brief.yaml --audio audio/interview.wav              # brief given
cutdown run … --until discover        # stop after drafting the brief
cutdown run … --force assemble        # re-run one stage
cutdown run … --revise                # answer yes after check
cutdown run … --no-revise             # keep the checked edit
cutdown run … --dry-run               # complete prompts, zero network calls
```

`run` skips any stage whose declared inputs, outputs and config slice are
unchanged since it last succeeded. `discover` only runs when there is no brief
(or when forced), so a corrected brief is never overwritten.

## Notes mode

The edit is already made and the clips are already exported. This reads them
back into something you can paste into an email.

```sh
cutdown notes ~/Desktop/interview-clips -o ep/interview/notes --guest "Alex Vale" --host "Morgan Reed"
cutdown notes                       # or omit the folder and be asked for it
```

```
<outdir>/
  notes.md          the document: digest, clip table, then each clip
  notes.txt         the same, wrapped plain text, for pasting anywhere
  notes.json        structured, if you want to render it differently
  transcripts/      one txt per clip, to send a single clip on its own
  build/notes/      per-clip WhisperX JSON, read JSON, cached summaries
```

Per clip you get a headline, a two-line summary, the anecdotes in it with a
timestamp range and how strong each one is, up to three pull quotes, and the
full transcript with a timestamp and a name on every paragraph. Above all of it,
one pass across every clip: what the interview turned out to be about, and which
clips are worth someone's time.

Same rules as the cutting pipeline, for the same reason:

| Rule | Where |
|---|---|
| The transcript is verbatim ASR. Only summaries and headings are written | `notesdoc.py`, said on the page itself |
| Models return paragraph IDs; code measures every timestamp printed | `notes.py:resolve_summary` |
| A pull quote not in the transcript word for word is dropped, not printed | `notes.py:locate` |
| Transcription is cached per clip, so iterating on prompts is cheap | `notes.py:run_notes` |

If the clips carry BWF stamps — a Pro Tools export usually does — they are put
in session order and each one says where in the interview it came from. Without
stamps it falls back to filename order and says so.

| | |
|---|---|
| `--host NAME` | what to call the interviewer; the guest comes from `--guest` |
| `--drop-fillers` | remove standalone um/uh from the transcript |
| `--resummarise` | ask again for the summaries, keep the transcripts |
| `--no-summary` | transcript only, no model call at all |
| `--order name` | filename order even when the BWF stamps are there |
| `--open` | reveal the folder when it is written |

Speaker names are worked out per clip: the model reads which voice is asking and
which is answering, and where there is no summary to read, the one who says the
most is taken to be the guest. Diarization labels do not survive across
separately transcribed clips, so this is a per-clip judgement — check it on a
clip that is mostly interviewer.

If WhisperX has already written its JSON when the terminal or Studio exits,
the next `transcribe`, `run`, or Studio attempt recovers that transcript rather
than processing the audio again. Use `cutdown transcribe --force …` (or force
the transcribe stage) only when you intentionally want a new transcription.

## Per-episode context

The models are reading ASR output, so a nudge about what is coming pays for
itself. Supply it on any command, or in the brief:

```sh
cutdown label ep/interview/build \
  --guest "Alex Vale" --host "Morgan" --show "Record Room" --episode interview \
  --song "The Lanterns - Paper Moon" --song "North Station - Low Tide" \
  --name "Casey Lane" \
  --context notes.md
```

It is merged (`build/context.json` ← `build/context.yaml` ← brief ← preset and
CLI flags), persisted, and injected into every prompt. In the context screen,
names and records are entered one per line. Saving replaces the displayed
fields, including emptied fields. The preview shows the text sent to every model.

### Presets

A preset is the part of the context that is the same every week: the show,
names, how the recording is made, what to keep, default segment lengths, and
config such as the speaker count. Load it, then add what is particular to today
with `--detail`. The two stay separate fields, so re-applying the preset does
not wipe the day's note.

```sh
cutdown preset new radio          # opens a template in $EDITOR
cutdown preset save radio --show "Record Room" --name Morgan --extras keep \
    --detail "The guests talk about each record, then we listen to it in the room."
cutdown preset save radio --from ep/interview --with-detail   # keep an episode's context
cutdown preset list | show radio | rm radio

cutdown run --audio today.wav --preset radio \
    --detail "four or five songs; keep the other chat, sorted into topics"
```

Stored as YAML in `~/.config/cutdown/presets/` (or `$CUTDOWN_PRESETS`):

```yaml
name: radio
show: Record Room
names: [Morgan]
instructions: |
  The guests talk about each record they picked, then we listen to it in the
  room. The records are not in the recording, so there is a gap where each played.
extras: true                                   # keep leftover chat as topic files
targets: {guest_pick: [90, 240], own_work: [60, 180]}
config: {transcribe: {max_speakers: 4}}        # overlaid on cutdown.toml
```

## One speaker per channel

If each speaker was on their own mic and channel, say so on the setup screen
("Voices in the file"), with `--channels`, or in a preset (`channels:`):

| setting | meaning |
|---|---|
| `mixed` (default) | both voices on both channels: transcribe once and diarize |
| `host-left` | host on the left channel, guest on the right |
| `guest-left` | guest on the left channel, host on the right |

```sh
cutdown today.wav --channels host-left
```

In split mode each channel is written out as mono (a byte copy, under a second
for a 30-minute file) and transcribed on its own with diarization off, then the
two are merged by time. The channel decides who is host and who is guest, so no
guessing from word counts or names. Mic bleed is handled: a word that shows up
on both channels at the same moment with the same text is kept only on the
channel where it is louder. The per-channel transcripts are cached in
`build/transcript/channels/`. Swapping `host-left` for `guest-left` re-runs
ingest only; changing between mixed and split re-transcribes. The audio that is
cut is always the original stereo master.

## How the recording's structure is used

**Sections.** Ingest splits the recording at long silences (`section_gap`,
default 20 s): when a record is played in the room and not recorded, the gap is
where it played. Sections longer than `section_max` (6 min) are split at their
widest pause. The section headers (`--- C03 (after 184s of silence) ---`, no
timestamps) go into every model-facing index, so discover, label, assemble and
topics all see where the song breaks fell.

**Discover plans the run.** It reads the instructions and the sections, drafts
the songs, and decides whether the leftover talk is kept (`extras.keep`) with
topic hints. A preset's `extras` setting overrides that decision.

**Topics.** After assemble, the talk no segment used (and that is not admin) is
grouped into topics by a model returning IDs. Code keeps each topic in recorded
order, places a unit in one topic at most, and drops topics under `topics.min_sec`
(20 s). Each topic becomes a file in `1-final/` next to the songs.

**Chunks.** Render also writes each section, unedited, to `3-chunks/`, named for
what fills it: the song or topic most of its talk was cut into, or
`production-talk` when it is mostly admin.

## Brief

```yaml
guest: "Alex Vale"
show: "Record Room"
episode: interview
master: audio/interview.wav
segments:
  - id: seg01
    anchor: { artist: "The Lanterns", title: "Paper Moon", kind: guest_pick }
    target_sec: [90, 240]
  - id: seg05
    anchor: { artist: "Alex Vale", title: "Blue Hour", kind: own_work }
    target_sec: [60, 180]
```

`cutdown discover` drafts this from the transcript with `evidence:` unit IDs
against each pick, so you can check its reading fast. Correct it before use —
artists and titles come from ASR.

In mixed recordings with exactly two speakers, ingest uses question rate when
one speaker asks at least five questions and at least twice the other's rate per
word. The lower rate is the guest. Otherwise it uses the existing name and word
count rules. In split mode channel settings remain authoritative; when the
question rates suggest reversal, the CLI and studio offer to swap them.
`build/roles.json` records the statistics and verdict.

## Editorial defaults

Assembly prioritises complete guest thoughts, preserving the original flow and
breathing room within answers. Host questions stay when needed for context or a
strong exchange; repeated setups and host restatements are removed first. Unit
boundaries are mechanical splits, so the model is explicitly asked to retain the
continuation of a sentence and check every jump and ending before returning.

The upper target has a **20-second completion allowance**, for finishing the
current point. It is recorded as `completion_extension`, not a length failure.
Set `[assemble] completion_grace_sec = 0` for a strict upper target. This is an
allowance, not an instruction to make every segment longer. Durations measure
source clips; the final file also includes the configured gaps between clips.

Code removes isolated `um`, `umm`, `uh`, `uhh`, `uhm`, `er`, and `erm`.
`ah` and `ahh` qualify only inside a sentence; `mm`, `hmm`, `yeah`, `you know`,
and `like` stay. An internal filler needs at least 0.12 seconds of measured
pause on both sides; retained pauses are capped at 0.45 seconds. `[fillers]`
configures `remove`, `min_gap`, and `max_pause`. Set `remove = false` to disable
automatic filler removal. Removed audio joins tightly; other edits retain
`render.stringout_gap_ms`.

After check, `Revise? [y/N]` offers one optional pass over warned pieces. Enter,
Esc, EOF, and non-interactive runs decline by default. The answer is remembered
for that check. Revision may add available context, remove units, reorder song
segments, and quote phrases to cut between measured pauses. Topics stay in
recorded order. It cannot rewrite speech or use a unit in two pieces. Length
failures can set `revision_rejected`; increased high warnings set
`revision_regressed`. Originals are kept in `build/pre-revise/`, and any error
restores them. Audio still needs auditioning for tone and timing.

Prompt changes (including `--prompts` overrides) invalidate their stage's cache.
Run the episode normally to rebuild affected stages; transcription remains
cached. Re-rendering alone applies safer trims but does not improve selection
or ordering: rerun assembly, topics, check and render for the full changes.

## Model routing

All editorial stages use OpenRouter by default, with separate model families for assembly and check. Use `cutdown init` to write a starter `cutdown.toml`. Run `cutdown models --match luna` to inspect the current catalog and routing.

```toml
[stages]
assemble = { provider = "openrouter", model = "deepseek/deepseek-v4.1-flash" }
check = { provider = "openrouter", model = "openai/gpt-6-luna" }
```

Set both `fallback_provider` and `fallback_model` on `assemble` to opt in to a full-stage retry through another route. Failed attempts remain under `runs/`. Revision inherits assembly routing unless you add a `revise` entry under `[stages]`.

`extra_body` merges provider options with stage overrides. Label, topics and check request low reasoning effort by default. Set `extra_body = {}` on the stage entry to clear the stage options. The default OpenRouter route requests `data_collection = "deny"`. See [OpenRouter routing preferences](https://openrouter.ai/docs/guides/routing/provider-selection) and [SECURITY.md](SECURITY.md).

OpenCode Zen (`zen`) and Go (`zengo`) are available when configured. Standalone model stages accept `--provider` and `--model`. For the available transports and stage options, see [PROCESS.md](PROCESS.md).

## Output

```
<outdir>/
  1-final/       seg01_paper-moon.wav       tight joins for removed audio; one
                 topic01_touring.wav          per song, then one per topic
  2-clips/       seg01_paper-moon/seg01_05_paper-moon_S0081-S0084.wav
                                              BWF-stamped, for Spot -> Timestamp
  3-chunks/      C02_04m51s_paper-moon.wav  the recording, unedited, per section
  manifest.csv   every clip and chunk (`kind`): source in/out, unit IDs, samples,
                 time_reference, reason, warnings, join (start/tight/edit)
  rejected.csv   what the model considered and did not use, and why
  report.md      where things are, durations, check warnings, what needs a human
  build/         units.jsonl units.txt timeline.json sections.json labels.jsonl
                 segments.json topics.json check.json context.json
  runs/          raw model I/O per stage
```

Folder names are configurable under `[render] dirs`; `chunks = false` (or
`render --no-chunks`) skips the chunks. Episodes rendered before this layout
keep their old `clips/` and `stringouts/` folders until re-rendered.

## Clip names

The filename is what you scan in Finder, so it carries the context rather than
making you look it up:

```
seg01_05_paper-moon_S0081-S0084.wav
 │     │       │            └─ unit IDs, to trace back to the transcript
 │     │       └─ the record this segment plays before
 │     └─ clip order within the segment
 └─ segment, in show order
```

Configurable — tokens are `seg`, `clip`, `title`, `artist`, `kind`, `ids`,
`first_id`, `last_id`, `episode`, `guest`. Empty tokens collapse rather than
leaving `__` gaps.

```toml
[render]
clip_template = "{seg}_{clip:02d}_{artist}_{title}_{ids}"
```

or per-run: `cutdown render … --clip-template '{seg}_{clip:02d}_{title}'`

The same context goes into the BWF `Description`, which Pro Tools shows in clip
info:

```
seg01 clip 1 | The Lanterns - Paper Moon | S0073 | master @00:09:44.928
```

## BWF timestamps

Clips carry a `bext` chunk whose `TimeReference` is the in-point in samples at
the master's rate, so **Spot → Timestamp** in Pro Tools drops each clip at its
original master position.

```sh
cutdown verify-bwf ep/interview/2-clips/*/*.wav
```

reads the chunk straight off disk, and cross-checks against `bwfmetaedit
--out-xml` when that happens to be installed.

## Audio handling

Clips are made by slicing the master's `data` chunk on sample boundaries and
writing a new header around those bytes. No decode, no encode: bit depth and
sample rate survive by construction and clip lengths are exact to the sample.

`ffmpeg` is therefore not on the cutting path for a WAV master. A master that
is not uncompressed WAV (MP3, M4A, FLAC, compressed WAV) is decoded once into
`build/master.wav` at its own rate, channel count and bit depth, then cut from
that. WhisperX also needs `ffmpeg`, and split-channel mode uses it for non-WAV
input.

Which `ffmpeg` runs is decided by `cutdown/media.py`, not by PATH alone.
Homebrew upgrades have left both `ffmpeg` and `ffmpeg@7` on this machine
aborting in dyld (missing `libx265` dylibs), so every candidate is run with
`-version` and the first that works is used: `--ffmpeg` / `render.ffmpeg`,
`$CUTDOWN_FFMPEG`, `ffmpeg` on PATH, the Homebrew and MacPorts locations, then
the static build shipped in the `imageio-ffmpeg` dependency, which Homebrew
never touches. WhisperX gets the chosen binary first on its PATH. If none
works, the error lists each candidate and why it failed.

Verified: `ffmpeg -c:a copy` seek is **not** sample-accurate (asked 240000
frames, got 241664), which is why it is not used.

## Prompts

`cutdown/prompts/*.md`, plain text, `{{placeholder}}` substitution. Point
`--prompts <dir>` at a copy to iterate without touching the package.
