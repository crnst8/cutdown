You are looking at the notes for every clip in one edited interview, and writing
the top of the page: the part someone reads before deciding whether to read the
rest, or forwards on its own.

You are not reading the transcripts here, only the per-clip notes. Work from
what they say.

## What to write

`title`: a short line naming what this interview turned out to be about. Not the
guest's name on its own — the thing that came out of it.

`summary`: three or four sentences. What the guest talked about, what came up
that was not expected, and what the strongest material is. Written for a
colleague who was not in the room and has thirty seconds.

`themes`: three to six short phrases that recur across the clips.

`highlights`: the clips actually worth someone's time, best first, at most five.
Give the clip's ID (`C01`, `C02`, …), one sentence on what is in it, and one line
on why it stands out. Do not list every clip. If only two are worth flagging,
flag two — a ranked list that turns out to be everything ranks nothing.

## Output

A single JSON object, no prose, no code fence:

{
  "title": "…",
  "summary": "…",
  "themes": ["…", "…"],
  "highlights": [
    {"clip": "C03", "what": "…", "why": "…"}
  ]
}
