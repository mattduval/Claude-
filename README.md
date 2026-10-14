# Zeevou contact deduplication

`zeevou_dedupe.py` finds duplicate guests/contacts in Zeevou and archives the ones that are provably safe.
Python 3 standard library only.

```
export ZEEVOU_TOKEN=...            # API bearer token
python3 zeevou_dedupe.py fetch     # read only: guests, contacts, bookings -> dedupe_data/
python3 zeevou_dedupe.py analyse   # read only: writes plan.json and review.csv
python3 zeevou_dedupe.py apply     # dry run
python3 zeevou_dedupe.py apply --execute
python3 zeevou_dedupe.py rollback  # restore from dedupe_data/backups
```

## What keeps it safe
- Matches only on normalised email or phone (UK number formats, Gmail dots/plus). Names alone never match.
- Relay/agent emails and any key shared by more than 5 records are skipped (e.g. shared agent contacts such as "Situ Guest Care").
- Names must agree; differing passport, date of birth or child flag blocks the match.
- Never deletes. Duplicates are archived, after saving a backup, then verified; any unexpected change is auto-restored.
- Only archives duplicates with no bookings (guest or booker), zero balance, no documents/notes/corporations, and no staff/investor/team/brand link.
- Everything else lands in `review.csv` for manual merging.

## Limits
Zeevou's API has no merge endpoint and `PUT` replaces whole records, so the tool does not move bookings between guests.
Duplicates with bookings are listed with the survivor to keep. Run `analyse` and review `review.csv` before `apply --execute`.
The API calls have not been run against the live system; test `apply --execute` on one or two records first.
