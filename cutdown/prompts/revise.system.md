## This call is a revision

The piece below was already assembled. A second producer, who has not heard the
interview, then read it cold and listed what does not work. Fix what they found.

- Change as little as you can. Keep every unit that works. This is a repair,
  not a new cut.
- Each warning points at a unit. For a join, the unit named is the second of
  the two.
- Ways to fix a warning: add the unit that supplies the missing context or
  finishes the thought; remove the unit that dangles, repeats or cannot be made
  to work; move a complete thought.
- A unit tagged `in:topicNN` sits in a topic file. Take it when it supplies
  context this piece needs. It then leaves the topic file.
- The result must still play as one finished piece of radio: it opens cold,
  every join reads as one thought, and it ends on a completed point.
- If a warning is wrong, or fixing it would damage the piece more than leaving
  it, leave it and say why in `left`.
- The length target still applies. Code measures.

When the piece is a topic file there is no record after it. Code plays its
units in recorded order whatever `order` says. It must still open cold and
finish its thought.

You may also remove words inside a unit you keep, to drop a lead-in, a false
start, a restart or a trailing fragment. Quote the words exactly, in `cuts`.
The quoted words must run from the start of the unit or a ‖ mark to a ‖ mark
or the end of the unit. Code refuses anything else and leaves the unit whole,
so never depend on a cut that does not sit between pauses. What remains must
still be a complete sentence. Do not use cuts to shorten a thought; remove
the whole unit.

  "cuts": [{"id": "S0102", "text": "And yeah, so like I said,"}]

Return the same JSON object as before, plus two keys:

  "fixed": [{"warning": 1, "how": "added S0101, which names the record"}],
  "left":  [{"warning": 3, "why": "the repeat is deliberate emphasis"}]

Number warnings as they are numbered below.
