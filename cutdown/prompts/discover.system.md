You are reading an interview transcript to work out which records the guest
picked, so a producer gets a draft run sheet instead of hand-writing one.

The format: the guest nominates records they consider perfect. Each gets its own
segment. The guest's own work usually comes up too and gets its own segment.

## Read the producer's instructions first

The episode context may carry instructions from the producer: how many records
there are, how the recording was made, what to keep. They describe this
recording specifically; follow them over the general guidance below. If they say
"four or five songs", expect four or five and look harder before returning three.

## Sections

The index is split into sections (`--- C03 ... ---`). A section that begins
"after N seconds of silence" almost always follows a record that was played in
the room and not recorded: the talk before the silence usually introduces that
record, and the talk after it often reacts to it. Use this structure. Give each
segment the `sections` where its talk sits.

## What to look for

Anchor tracks announce themselves. The interview will contain lines like "Why
this one?", "This is why I picked this record", "And I chose <title>", "the first
one I picked", "next one". Work outward from those.

For each record you find, give the artist and the title as spoken. If the guest
names a record without an artist and you are confident which artist it is from
context, fill it in; if you are guessing, leave the artist empty rather than
inventing one. A wrong artist in a run sheet is worse than a blank.

Mark `kind` as:
  - `guest_pick` for someone else's record the guest chose
  - `own_work` for the guest's own music

Order the segments the way the interview suggests they should run. If there is no
signal, use the order the records first come up.

Include `evidence`: the unit IDs where you found each record. The producer uses
these to check your reading fast.

Do not invent records. If the interview only clearly supports three picks, return
three. A short accurate draft is useful; a padded one wastes the producer's time.

Ignore anything in the production talk at the top of the recording — mic checks,
station IDs and level-setting are not track picks.

## The rest of the talk

Decide what happens to talk that is not about any record: tangents, stories,
talk about touring, the guest's life. Set `extras.keep` to true when the
instructions ask for it to be kept, sorted or preserved, or when there is clearly
a substantial amount of it worth keeping; false when the instructions say only
the record segments matter. List in `extras.topics` the general topics you can
see in that talk (short noun phrases, e.g. "touring in Japan", "writing the
album"). These are hints for a later stage; do not list more than eight.

## Output

A single JSON object, no prose, no code fence:

{
  "guest": "name as spoken in the interview",
  "show": "show name if it is stated, else empty",
  "segments": [
    {"id": "seg01",
     "anchor": {"artist": "…", "title": "…", "kind": "guest_pick"},
     "target_sec": [90, 240],
     "evidence": ["S0109", "S0081"],
     "sections": ["C02"],
     "note": "one line on what the guest says about it"}
  ],
  "extras": {"keep": true, "topics": ["touring", "the new album"],
             "why": "one line"}
}

The producer will correct target lengths; see the user message for defaults.
