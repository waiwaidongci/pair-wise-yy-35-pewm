import tempfile, unittest
from pathlib import Path
from src import rules
from src.audit import utc_now
from src.domain import ConflictError, NotFoundError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES
HOLD_PAYLOAD={"case_no":"CASE-1","reason":"医疗纠纷调查","custodian":"法务-张三","start_at":"2000-01-01T00:00:00Z","end_at":"2999-01-01T00:00:00Z"}
class RetentionTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.repo=Repository(str(Path(self.tmp.name)/"test.db")); self.service=Service(self.repo)
    def tearDown(self): self.repo.close(); self.tmp.cleanup()
    def _closed_item(self,severity,ref):
        item=self.service.create_item({"title":"retention item","description":"retention flow","severity":severity,"quantity":1,"threshold":10,"external_ref":ref},"creator",'dosimetrist')
        current=item
        for target in STATES[1:]:
            current=self.service.transition(current["id"],target,current["version"],"reviewer",TRANSITION_ROLES[target][0])
        return current
    def _expire(self,item_id):
        with self.repo.conn: self.repo.conn.execute("UPDATE items SET retention_until=? WHERE id=?",("2000-01-01T00:00:00+00:00",item_id))
    def test_close_generates_retention_until_by_severity(self):
        low=self._closed_item('low',"RET-1"); critical=self._closed_item('critical',"RET-2")
        self.assertIsNotNone(low["retention_until"]); self.assertGreater(critical["retention_until"],low["retention_until"])
        self.assertEqual(rules.retention_deadline('low',"2026-09-26T00:00:00+00:00"),"2027-09-26T00:00:00+00:00")
        events=self.service.audit("viewer",low["id"]); self.assertEqual(events[-1]["detail"]["retention_until"],low["retention_until"])
    def test_effective_hold_blocks_purge(self):
        item=self._closed_item('low',"RET-H1"); self._expire(item["id"])
        self.service.place_hold(item["id"],HOLD_PAYLOAD,"officer",'radiation_officer')
        holds=self.service.list_holds(item["id"],"viewer")
        self.assertEqual(holds[0]["case_no"],"CASE-1"); self.assertEqual(holds[0]["custodian"],"法务-张三"); self.assertTrue(holds[0]["effective"])
        result=self.service.purge_expired("officer",'health_physicist')
        self.assertEqual(result["count"],0); self.assertEqual(self.service.get_item(item["id"],"viewer")["id"],item["id"])
    def test_purge_after_release_and_cascade(self):
        item=self._closed_item('low',"RET-P1")
        self.service.add_record(item["id"],{"kind":"evidence","detail":"to be cascaded","status":"closed","external_ref":"EV-P1"},"recorder",'radiation_officer')
        self._expire(item["id"])
        hold=self.service.place_hold(item["id"],HOLD_PAYLOAD,"officer",'radiation_officer')
        self.service.release_hold(hold["id"],"officer",'health_physicist')
        result=self.service.purge_expired("officer",'radiation_officer')
        self.assertEqual(result["purged"],[item["id"]])
        with self.assertRaises(NotFoundError): self.service.get_item(item["id"],"viewer")
        self.assertEqual(self.repo.conn.execute("SELECT COUNT(*) AS n FROM records WHERE item_id=?",(item["id"],)).fetchone()["n"],0)
        self.assertTrue(self.repo.verify_audit_chain())
    def test_expired_hold_does_not_block(self):
        item=self._closed_item('low',"RET-E1"); self._expire(item["id"])
        self.service.place_hold(item["id"],{"case_no":"CASE-OLD","reason":"已结案的纠纷","custodian":"法务-李四","start_at":"2000-01-01T00:00:00Z","end_at":"2001-01-01T00:00:00Z"},"officer",'radiation_officer')
        self.assertFalse(self.service.list_holds(item["id"],"viewer")[0]["effective"])
        self.assertEqual(self.service.purge_expired("officer",'health_physicist')["purged"],[item["id"]])
    def test_unclosed_or_unexpired_items_not_purged(self):
        open_item=self.service.create_item({"title":"open","description":"not closed","severity":'low',"quantity":1,"threshold":10,"external_ref":"RET-O1"},"creator",'dosimetrist')
        closed=self._closed_item('critical',"RET-O2")
        self.assertEqual(self.service.purge_expired("officer",'radiation_officer')["count"],0)
        self.assertEqual(self.service.get_item(open_item["id"],"viewer")["status"],STATES[0])
        self.assertEqual(self.service.get_item(closed["id"],"viewer")["status"],STATES[-1])
    def test_recheck_aborts_and_rolls_back_on_change(self):
        first=self._closed_item('low',"RET-R1"); second=self._closed_item('low',"RET-R2")
        self._expire(first["id"]); self._expire(second["id"])
        candidates=self.repo.purge_candidates(utc_now()); self.assertEqual(len(candidates),2)
        self.service.place_hold(second["id"],HOLD_PAYLOAD,"officer",'radiation_officer')
        with self.assertRaises(ConflictError): self.repo.purge_batch(candidates,utc_now(),"officer")
        self.assertEqual(self.service.get_item(first["id"],"viewer")["id"],first["id"])
        self.assertEqual(self.service.get_item(second["id"],"viewer")["id"],second["id"])
        stale=self.repo.purge_candidates(utc_now()); self.assertEqual([c["id"] for c in stale],[first["id"]])
        with self.repo.conn: self.repo.conn.execute("UPDATE items SET retention_until=? WHERE id=?",("2999-01-01T00:00:00+00:00",first["id"]))
        with self.assertRaises(ConflictError): self.repo.purge_batch(stale,utc_now(),"officer")
        self.assertEqual(self.service.get_item(first["id"],"viewer")["id"],first["id"])
    def test_hold_validation_and_permissions(self):
        item=self._closed_item('high',"RET-V1")
        with self.assertRaises(PermissionDenied): self.service.place_hold(item["id"],HOLD_PAYLOAD,"attacker",'viewer')
        with self.assertRaises(ValidationError): self.service.place_hold(item["id"],{"case_no":"C","reason":"r","custodian":"k","start_at":"2026-01-02T00:00:00Z","end_at":"2026-01-01T00:00:00Z"},"officer",'radiation_officer')
        with self.assertRaises(ValidationError): self.service.place_hold(item["id"],{"case_no":"C","reason":"r","custodian":"k","start_at":"not-a-time","end_at":"2026-01-01T00:00:00Z"},"officer",'radiation_officer')
        hold=self.service.place_hold(item["id"],HOLD_PAYLOAD,"officer",'health_physicist')
        with self.assertRaises(ConflictError): self.service.place_hold(item["id"],HOLD_PAYLOAD,"officer",'health_physicist')
        self.service.release_hold(hold["id"],"officer",'radiation_officer')
        with self.assertRaises(ConflictError): self.service.release_hold(hold["id"],"officer",'radiation_officer')
        with self.assertRaises(PermissionDenied): self.service.purge_expired("attacker",'dosimetrist')
if __name__=="__main__": unittest.main()
