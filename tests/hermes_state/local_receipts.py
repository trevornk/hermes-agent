"""Receipt fixtures that include their transactional listing projection."""
import json

from hermes_state_local import POLICY_PREFIX, sync_lineage_index


def store_receipt(conn, owner, lineage, **fields):
    receipt = dict(session_id=owner, lineage=lineage, **fields)
    conn.execute('INSERT OR REPLACE INTO state_meta(key,value) VALUES(?,?)',
                 (POLICY_PREFIX + owner, json.dumps(receipt)))
    sync_lineage_index(conn, owner, lineage)
    return receipt
