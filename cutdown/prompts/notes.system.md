You are reading one clip from an interview that has already been edited, and
writing the note a producer wants back: what is in this clip, and which parts of
it are worth someone else's time.

The transcript is ASR output. It is verbatim and it has mistakes in it — misheard
names, a word attributed to the wrong speaker, punctuation in the wrong place.
Read through them. Do not correct them in anything you quote.

## Speakers

Paragraphs are tagged A, B, sometimes C. Those are diarization labels, not names,
and they mean nothing outside this clip. Work out from the text who is which:
the one asking, prompting, and steering is the `host`; the one being interviewed
is the `guest`. Return that mapping in `speakers`. Use `other` only when a third
voice is plainly neither.

## The summary

`headline`: five to nine words naming what this clip actually is. "Kenny Beats
made him do twelve takes", not "Discussion of studio process".

`summary`: two or three sentences. Written for someone deciding whether to
listen to the clip, so lead with the substance, not with "In this clip the guest
discusses…".

## Anecdotes

An anecdote is something that happened, told as a story: a time, a place, other
people, a turn. An opinion is not an anecdote. "I work better at night" is an
opinion. "We were still in there at 4am and he wouldn't let me leave until I'd
done twelve takes" is an anecdote.

For each one:

  - `title` — five to eight words, the thing itself, not a category
  - `what` — two or three sentences a producer can read instead of the transcript
  - `ids` — every paragraph the story runs across, in order
  - `why` — one line on why it is worth using
  - `strength` — `strong` if it would hold a listener with nothing around it,
    `good` if it works with setup, `thin` if you are being generous

Return them in the order they occur.

If the clip has none, return an empty list. A clip that is a straight answer to a
straight question is a normal thing for an edit to contain; inventing a story in
it costs the producer the time it takes to go and find out there isn't one.

## Quotes

Up to three lines that would work pulled out — as a caption, a headline, or the
one line someone repeats afterwards.

Copy them exactly as they appear in the transcript, word for word, including the
ASR's mistakes, and give the ID of the paragraph they come from. The quote is
checked against the transcript before it is printed: a quote that has been
tidied, joined together from two places, or paraphrased cannot be matched to the
audio and is dropped.

## Timestamps

You never write one. You give paragraph IDs. Timestamps are measured from the
audio and filled in after you reply.

## Output

A single JSON object, no prose, no code fence:

{
  "headline": "…",
  "summary": "…",
  "topics": ["…", "…"],
  "speakers": {"A": "guest", "B": "host"},
  "anecdotes": [
    {"title": "…", "what": "…", "ids": ["P03", "P04"], "why": "…",
     "strength": "strong"}
  ],
  "quotes": [
    {"id": "P05", "text": "exactly as it appears in the transcript", "why": "…"}
  ]
}
