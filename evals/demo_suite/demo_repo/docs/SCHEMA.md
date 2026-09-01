# Schema

## events (partitioned by `ts`, monthly)

| column | type | note |
|---|---|---|
| `stream_id` | text | partition-local grouping key |
| `seq` | bigint | **per-stream**, not globally unique |
| `ts` | timestamptz | UTC. Partition key — every query needs a `ts >= ` bound |
| `payload` | jsonb | |

Primary key is `(stream_id, seq)`. There is no unique index on `seq` alone.
