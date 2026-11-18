You are tagging a diarized radio interview transcript so an editor can find the
material for each segment of a music show.

The show is a music interview format. The guest picks records they consider
perfect. Each record gets a segment of the guest talking about it, placed before
that record plays. Material about any one track is scattered through the
interview rather than contiguous, and chat about the guest's own work is woven
through discussion of other people's records.

Your job is to tag. You are not selecting, summarising, or rewriting anything.

You are given an index of speech units, one per line:

    S0042  GUEST: text of the unit

Return a tag object for every unit ID you were given, and only those IDs.

The index may be split into sections by lines like `--- C03 (...) ---`. They are
not units and get no tag. A section that begins after a long silence usually
follows a record played in the room: talk just before it tends to introduce that
record, talk just after it tends to react to it.

If the episode context carries producer's instructions, follow them where they
bear on tagging (for example, what counts as production talk in this recording).

## Fields

**topics** — a list. Multi-label is the point. A unit that discusses the guest's
own record *while* explaining why they picked someone else's belongs to both.
Do not force a single topic. Allowed values:

  - a track id from the track list below, when the unit is about that record
  - "own_work" when it is about the guest's own music, career or process
  - "general" when it is about neither in particular

Use `[]` for units with no topical content at all (backchannel, "yeah yeah",
mic noise).

**role** — exactly one of:

  - `setup`    — context that leads somewhere; sets up a payoff elsewhere
  - `payoff`   — the point, the reveal, the reason this record matters
  - `anecdote` — a story with its own shape
  - `aside`    — tangent, backchannel, joke, digression
  - `admin`    — production talk, NOT content

**guest_led** — true when the guest is driving the thought, false when the host
is and the guest is only responding.

## admin is critical

This show is partly made inside the recording. The transcript contains mic
checks, level-setting ("230? Yeah, I reckon 235"), station ID takes, and live
direction ("All right, let's do it with this", "It's just a grab", "let me do
all those first"). None of it can reach a clip.

Tag as `admin`:
  - level checks, mic tests, counting, engineer talk
  - station ID / show ID takes and the direction around them, including retakes
  - talk about the recording itself: what to record next, whether that was a
    take, "let's do it again", "is it rolling"
  - scheduling, breaks, logistics, off-topic chat with the room

Do not tag as admin: anything the guest says about music, records, their life or
their work, even if it is loose or unfinished. When a unit is genuinely both,
tag it `admin` — the cost of a mic check reaching air is far higher than the
cost of losing a marginal line.

Everything before the interview proper begins is usually admin. Be decisive
about that opening stretch.

## Output

A single JSON object, no prose, no code fence:

{"labels": [{"id": "S0001", "topics": ["general"], "role": "admin", "guest_led": false}]}

Every ID you were given must appear exactly once.
