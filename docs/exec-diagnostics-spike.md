# Exec Diagnostics Spike

Status: completed, 2026-06-08.

This spike captured the Pantograph shapes needed for `/exec` structured
messages and for the warm-pool proof certification decision.

## Raw Cases

### `load_sorry` parse failure

```json
{
  "operation": "load_sorry",
  "exception_class": "pantograph.message.ServerError",
  "payload": {
    "desc": "<anonymous>:2:7: error: unexpected end of input\n",
    "error": "io"
  },
  "exec_messages": [
    {
      "severity": "error",
      "data": "unexpected end of input",
      "pos": { "line": 2, "col": 7 }
    }
  ]
}
```

### `load_sorry` type failure

```json
{
  "operation": "load_sorry",
  "exception_class": "pantograph.message.ServerError",
  "payload": {
    "desc": "<anonymous>:2:2: error: Type mismatch\n  False\nhas type\n  Prop\nbut is expected to have type\n  True\n",
    "error": "io"
  },
  "exec_messages": [
    {
      "severity": "error",
      "data": "Type mismatch\n  False\nhas type\n  Prop\nbut is expected to have type\n  True",
      "pos": { "line": 2, "col": 2 }
    }
  ]
}
```

### `goal_tactic` failure

```json
{
  "operation": "goal_tactic",
  "exception_class": "pantograph.message.TacticFailure",
  "payload": {
    "hasSorry": false,
    "hasUnsafe": false,
    "parseError": "<Pantograph>:1:1: unknown tactic"
  },
  "exec_messages": [
    {
      "severity": "error",
      "data": "unknown tactic",
      "pos": { "line": 1, "col": 1 }
    }
  ]
}
```

### Successful tactic with message

Running a tactic that emits a suggestion, such as `simp?`, returns structured
`pantograph.message.Message` objects on the child goal state.

```json
{
  "operation": "goal_tactic",
  "message_shape": {
    "severity": "Severity.INFORMATION",
    "pos": { "line": 0, "column": 0 },
    "pos_end": { "line": 0, "column": 5 },
    "data": "Try this:\n  [apply] simp only"
  },
  "exec_messages": [
    {
      "severity": "info",
      "data": "Try this:\n  [apply] simp only",
      "pos": { "line": 1, "col": 0 },
      "end_pos": { "line": 1, "col": 5 }
    }
  ]
}
```

Pantograph reports tactic-string message positions with line `0`; the wire API
normalizes source lines to 1-based while keeping columns 0-based.

## Mapping

| Pantograph source | Severity | Position | `ExecMessage` mapping |
| --- | --- | --- | --- |
| `pantograph.message.Message` | `Severity.INFORMATION`, `WARNING`, `ERROR`, `TRACE` | `pos`, `pos_end` fields when present | Normalize severity to `info`, `warning`, `error`, `trace`; convert `Position(line,column)` to `ExecPos(line,col)` |
| `ServerError.args[0]["desc"]` | Embedded in text, usually `error` | Embedded as `<anonymous>:line:col:` | Parse the prefix, preserve the remaining text as `data` |
| `TacticFailure.args[0]["parseError"]` | Error | Embedded as `<Pantograph>:line:col:` | Parse the prefix, preserve the remaining text as `data` |
| Other payload text | Caller default | None | Return an unpositioned `ExecMessage` |

`load_sorry` positions are body-relative after the exec router splits imports
from the snippet. The router adds the header line offset before returning
messages to clients that submitted full code.

## Verify Equivalence

Stepping a Pantograph state is enough to know each tactic elaborated in the
current state, and tactic-generated `sorry` is rejected while stepping. It is
not enough to certify reward-grade acceptance because it does not enumerate the
closed theorem's axiom dependencies.

Pantograph can compile a standalone proof on an existing warm worker with
`check_compile_async(..., new_constants=True)`. Appending
`#print axioms <theorem_name>` yields structured info/warning messages:

```json
[
  { "case": "clean", "message": "'t' does not depend on any axioms" },
  { "case": "axiom-tainted", "message": "'t' depends on axioms: [bad]" },
  { "case": "sorry-tainted", "message": "'t' depends on axioms: [sorryAx]" }
]
```

The minimal certifier is therefore:

1. Compile the assembled proof body on a warm Pantograph worker.
2. Append `#print axioms <theorem_name>`.
3. Reject Lean errors, `sorry`/`sorryAx`, and any axiom outside the item allow-list.

Verdict: `/exec/verify` is path C viable on the warm Pantograph pool, but it
must use full standalone compile plus axiom printing. A completed stepped state
alone is not a sufficient certificate.
