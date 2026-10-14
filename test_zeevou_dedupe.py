import unittest

import zeevou_dedupe as z


def g(i, first, last, email=None, phone=None, **kw):
    return {"id": i, "first_name": first, "last_name": last, "email": email, "mobile_number": phone,
            "archive": False, **kw}


def run(guests, bookings=(), contacts=()):
    return z.analyse(list(guests), list(contacts), list(bookings))


class Tests(unittest.TestCase):
    def test_phone_formats_match(self):
        self.assertEqual(z.norm_phone("07714 401517"), z.norm_phone("+44 7714 401517"))
        self.assertEqual(z.norm_phone("+44 (0)7714 401517"), z.norm_phone("07714401517"))

    def test_gmail_aliases(self):
        self.assertEqual(z.norm_email("A.B+x@gmail.com"), "ab@gmail.com")

    def test_relay_email_ignored(self):
        self.assertIsNone(z.norm_email("x@guest.booking.com"))

    def test_simple_duplicate_is_safe_archive(self):
        p = run([g(1, "James", "Read", "j@x.com"), g(2, "james", "Read", "J@x.com")])
        self.assertEqual(len(p["safe_archive"]), 1)

    def test_keeps_record_with_bookings(self):
        bk = [{"guest": {"id": 2}}]
        p = run([g(1, "James", "Read", "j@x.com"), g(2, "James", "Read", "j@x.com")], bk)
        self.assertEqual(p["safe_archive"][0]["keep"], 2)

    def test_duplicate_with_bookings_goes_to_review(self):
        bk = [{"guest": {"id": 1}}, {"guest": {"id": 2}}]
        p = run([g(1, "James", "Read", "j@x.com"), g(2, "James", "Read", "j@x.com")], bk)
        self.assertEqual(p["safe_archive"], [])
        self.assertEqual(len(p["manual_review"]), 1)

    def test_person_booking_counts_as_usage(self):
        bk = [{"guest": {"id": 9}, "person_booking": {"id": 1}}]
        p = run([g(1, "A", "B", "a@x.com"), g(2, "A", "B", "a@x.com")], bk)
        self.assertEqual(p["safe_archive"][0]["duplicate"], 2)
        self.assertEqual(p["safe_archive"][0]["keep"], 1)

    def test_same_email_different_names_not_merged(self):
        p = run([g(1, "Anna", "Smith", "f@x.com"), g(2, "Bob", "Jones", "f@x.com")])
        self.assertEqual(p["safe_archive"] + p["manual_review"], [])
        self.assertEqual(len(p["key_conflicts"]), 1)

    def test_passport_conflict_blocks(self):
        p = run([g(1, "A", "B", "a@x.com", passport_id="1"), g(2, "A", "B", "a@x.com", passport_id="2")])
        self.assertEqual(p["safe_archive"], [])

    def test_shared_key_skipped(self):
        gs = [g(i, "A", "B", "a@x.com") for i in range(1, 8)]
        p = run(gs)
        self.assertEqual(p["safe_archive"], [])
        self.assertEqual(len(p["skipped_shared_keys"]), 1)

    def test_protected_never_archived(self):
        c = [{"guest": "/apis/guests/2", "staff": "/apis/staff/5"}]
        p = run([g(1, "A", "B", "a@x.com"), g(2, "A", "B", "a@x.com")], contacts=c)
        self.assertEqual(p["safe_archive"][0]["duplicate"], 1)

    def test_balance_and_documents_block(self):
        p = run([g(1, "A", "B", "a@x.com", balance={"value": 50}), g(2, "A", "B", "a@x.com", guest_documents=[1])])
        self.assertEqual(p["safe_archive"], [])
        self.assertEqual(len(p["manual_review"]), 1)

    def test_richer_record_is_kept(self):
        p = run([g(1, "A", "B", "a@x.com"), g(2, "A", "B", "a@x.com", guest_documents=[1])])
        self.assertEqual(p["safe_archive"][0]["duplicate"], 1)

    def test_archived_ignored(self):
        p = run([g(1, "A", "B", "a@x.com"), g(2, "A", "B", "a@x.com", archive=True)])
        self.assertEqual(p["safe_archive"] + p["manual_review"], [])


if __name__ == "__main__":
    unittest.main()
