Your selection for {{segment_id}} is the wrong length.

Measured duration of your selection: {{measured}} seconds.
Target: {{target_lo}} to {{target_hi}} seconds.
You are {{delta}} seconds {{direction}}.

These are measured from the master recording, not estimated. Do not argue with
the figure and do not try to estimate durations yourself.

Your current selection, in order, with each unit's measured length:

{{selection}}

{{advice}}

Preserve complete guest thoughts and their endings. Never shorten an answer
mid-sentence or add repeated ideas/host padding to satisfy the number. If the
available material cannot meet the range cleanly, return the strongest complete
cut and explain why in reason; code will flag its length for review.

Return a corrected JSON object in the same shape as before. Keep what works —
this is an edit to the selection, not a fresh start. Anything you drop should
move into `rejected` with a reason.
