"""Preregistered raw-capture consent checks and atomic consumption; no raw IO.

The closed ``raw-capture`` scope binds one proposed source operation, its kind,
one stream, a byte cap and a generated basename inside that operation's run.
The basename is a logical destination only: no path is opened or created here.
Maven uses ``maven-output``; advisory uses ``advisory-response``. Output names
are ``raw-<canonical UUID>.bin`` and the maximum capture is 64 MiB.

``validate_pair`` checks two independently supplied approvals and observations
without modifying the report. Its return value is NOT executable authority and
consumes nothing. ``consume_pair`` publishes BOTH consumed transitions and
started audits in ONE RunStore commit before a tee or source execution begins.
Calling the existing single-operation consume twice would not be atomic.

``build_capture_proposal`` and registration use the canonical operations
builder and dispatcher. Registration rechecks the source and reservations
against the current report; no duplicate output or source registration is
accepted, including registrations whose capture was denied. Renewal requires
a new source operation. Single-operation consumption refuses raw captures and
sources with a non-denied capture registration. Denying a capture permits the
source to run normally without capture, using its own approval.
Existing source executors still need a paired-consumption entry point; they
cannot execute a source with a pending capture through their ordinary path.

Approval is not sanitization. Raw output can contain credentials. No affirmative
approval record, raw file, network request or process is created here.
"""

import copy
import re
import uuid
from types import SimpleNamespace

from mdi import operations
from mdi.report import _json_equal, validate_report


MAX_CAPTURE_BYTES = 64 * 1024 * 1024
_SCOPE_FIELDS = frozenset({
    'kind', 'source_operation_id', 'source_kind', 'stream', 'max_bytes', 'output_name',
})
_STREAMS = {'maven': 'maven-output', 'advisory': 'advisory-response'}
_OUTPUT_NAME = re.compile(
    r'raw-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.bin\Z'
)
_DISCLOSURE = (
    'Raw capture is not sanitized and may contain credentials or other sensitive data. '
    'Maven diagnostics routinely carry secrets. The captured bytes remain in an '
    'owner-only 0600 file inside this run; nothing is uploaded or printed. '
    'Source and capture approval must both be consumed atomically before execution '
    'and before retaining any raw bytes. Capture after execution is not supported.'
)


class CaptureConsentError(operations.OperationsError):
    """A capture request or its paired approvals do not satisfy the contract."""


def _id(value, label):
    if (not isinstance(value, str) or not value or len(value) > 256
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise CaptureConsentError(f'{label} must be bounded nonempty identifier text')
    return value


def validate_scope(scope, path='scope'):
    """Validate the exact logical capture scope; safe for dispatcher reuse."""
    if not isinstance(scope, dict) or set(scope) != _SCOPE_FIELDS:
        raise CaptureConsentError(f'{path} must contain exactly the raw-capture scope fields')
    if scope['kind'] != 'raw-capture':
        raise CaptureConsentError(f'{path}.kind must be raw-capture')
    _id(scope['source_operation_id'], f'{path}.source_operation_id')
    source_kind = scope['source_kind']
    if not isinstance(source_kind, str) or source_kind not in _STREAMS:
        raise CaptureConsentError(f'{path}.source_kind must be maven or advisory')
    if scope['stream'] != _STREAMS[source_kind]:
        raise CaptureConsentError(f'{path}.stream does not match the source kind')
    cap = scope['max_bytes']
    if type(cap) is not int or not 1 <= cap <= MAX_CAPTURE_BYTES:
        raise CaptureConsentError(f'{path}.max_bytes must be an integer from 1 through {MAX_CAPTURE_BYTES}')
    name = scope['output_name']
    if not isinstance(name, str) or not _OUTPUT_NAME.fullmatch(name):
        raise CaptureConsentError(f'{path}.output_name must be a generated raw UUID basename')


def capture_scope(source_operation_id, source_kind, *, max_bytes, output_name=None):
    """Return a bounded request scope, not an approval or an operation record."""
    if not isinstance(source_kind, str) or source_kind not in _STREAMS:
        raise CaptureConsentError('source_kind must be maven or advisory')
    scope = {
        'kind': 'raw-capture',
        'source_operation_id': source_operation_id,
        'source_kind': source_kind,
        'stream': _STREAMS[source_kind],
        'max_bytes': max_bytes,
        'output_name': output_name if output_name is not None else f'raw-{uuid.uuid4()}.bin',
    }
    validate_scope(scope)
    return scope


def _source(report, scope):
    validate_scope(scope)
    source = operations._find_operation(report, scope['source_operation_id'])
    if source['run_id'] != report['run_id']:
        raise CaptureConsentError('source operation belongs to a different run')
    if source['state'] != 'proposed':
        raise CaptureConsentError('source must still be proposed; a fresh source operation is required')
    if source['scope'].get('kind') != scope['source_kind']:
        raise CaptureConsentError('capture source kind differs from the proposed source')
    operations._validate_proposal_record(source)
    return source


def validate_capture_record(record):
    """Check capture-specific proposal policy in the canonical dispatcher."""
    validate_scope(record['scope'])
    if record['input_identities']:
        raise CaptureConsentError('capture has no local input identities')
    if record['id'] == record['scope']['source_operation_id']:
        raise CaptureConsentError('capture and source operation IDs must differ')
    if _DISCLOSURE not in record['disclosure']:
        raise CaptureConsentError('capture disclosure must state sensitivity, retention and dual consent')


def validate_registration(report, scope):
    """Check a capture request against a report without persisting anything."""
    validate_report(report)
    source = _source(report, scope)
    for record in report['operations']:
        other = record['scope']
        if other.get('kind') != 'raw-capture':
            continue
        validate_scope(other)
        if other['output_name'] == scope['output_name']:
            raise CaptureConsentError('capture output name is already reserved in this run')
        if other['source_operation_id'] == source['id']:
            raise CaptureConsentError('source already has a capture registration; create a fresh source')
    return copy.deepcopy(scope)


def build_capture_proposal(report, source_operation_id, *, max_bytes,
                           tool_identity, operation_id=None, output_name=None):
    """Build a capture through the canonical closed-scope dispatcher.

    ``tool_identity`` must identify the future trusted capture implementation,
    not an untrusted repository tool. The source record already immutably binds
    its own command, inputs and tool to its run-local operation ID.
    """
    validate_report(report)
    source = operations._find_operation(report, source_operation_id)
    scope = capture_scope(source_operation_id, source['scope'].get('kind'),
                          max_bytes=max_bytes, output_name=output_name)
    validate_registration(report, scope)
    if operation_id == source_operation_id:
        raise CaptureConsentError('capture and source operation IDs must differ')
    disclosure = (
        f'{_DISCLOSURE} Source operation: {source_operation_id}. '
        f'Stream: {scope["stream"]}. Limit: {max_bytes} bytes. '
        f'Run-local destination: {scope["output_name"]}.'
    )
    return operations.build_proposal(
        'raw-capture', scope, [], tool_identity, disclosure, report['run_id'], operation_id,
    )


def validate_pair(report, capture_operation_id, source_approval, capture_approval, *,
                  observed_source_inputs, observed_source_tool, observed_capture_tool):
    """Check the pair against one immutable snapshot; return no authority.

    These checks neither write nor consume. consume_pair reloads and validates
    the current snapshot, then commits with the caller's expected revision.
    RunConflict rejects stale or conflicting publication before execution;
    a prior successful validation never grants authority to a later snapshot.
    """
    validate_report(report)
    _id(capture_operation_id, 'capture_operation_id')
    capture = operations._find_operation(report, capture_operation_id)
    if capture['state'] != 'proposed':
        raise CaptureConsentError('capture is not proposed; a fresh source and capture are required')
    validate_scope(capture['scope'])
    source = _source(report, capture['scope'])
    if source['id'] == capture['id']:
        raise CaptureConsentError('capture and source operation IDs must differ')
    if capture['audit'] or capture['evidence_ids'] or capture['input_identities']:
        raise CaptureConsentError('a fresh capture has no audit, evidence or local input identities')
    operations._check_tool_identity(capture['tool_identity'])
    operations._text(capture['disclosure'], operations.MAX_DISCLOSURE, 'capture.disclosure')
    if _DISCLOSURE not in capture['disclosure']:
        raise CaptureConsentError('capture disclosure must state sensitivity, retention and dual consent')
    for record in report['operations']:
        if record['id'] == capture['id'] or record['scope'].get('kind') != 'raw-capture':
            continue
        other = record['scope']
        validate_scope(other)
        if (other['output_name'] == capture['scope']['output_name']
                or other['source_operation_id'] == source['id']):
            raise CaptureConsentError('capture registration is not unique within the run')
    # Existing approval validation needs only the run_id from its store argument.
    run = SimpleNamespace(run_id=report['run_id'])
    operations._validate_approval(source_approval, run, source['id'], source)
    operations._validate_approval(capture_approval, run, capture['id'], capture)
    if not _json_equal(observed_source_inputs, source['input_identities']):
        raise operations.ApprovalError('observed source input identities differ from the proposal')
    if not _json_equal(observed_source_tool, source['tool_identity']):
        raise operations.ApprovalError('observed source tool identity differs from the proposal')
    if not _json_equal(observed_capture_tool, capture['tool_identity']):
        raise operations.ApprovalError('observed capture tool identity differs from the proposal')
    return {
        'run_id': report['run_id'],
        'revision': report['revision'],
        'source_operation_id': source['id'],
        'capture_operation_id': capture['id'],
        'scope': copy.deepcopy(capture['scope']),
        'consumed': False,
    }


def consume_pair(store, capture_operation_id, source_approval, capture_approval, *,
                 observed_source_inputs, observed_source_tool,
                 observed_capture_inputs, observed_capture_tool, expected_revision):
    """Consume both approvals in one revision; never launch or write raw bytes.

    A failure to publish returns no descriptor. An interrupt or uncertain
    publication can leave both consumed, never safely retryable. Executors must
    reload that exact revision and validate their own policy before execution;
    this descriptor is not a reusable execution entry point.
    """
    report = store.load()
    if report['run_id'] != store.run_id:
        raise CaptureConsentError('loaded report belongs to a different run')
    if not _json_equal(observed_capture_inputs, []):
        raise operations.ApprovalError('observed capture input identities must be an empty list')
    checked = validate_pair(
        report, capture_operation_id, source_approval, capture_approval,
        observed_source_inputs=observed_source_inputs, observed_source_tool=observed_source_tool,
        observed_capture_tool=observed_capture_tool)
    stamp = operations._now()
    for identifier, approval in ((checked['source_operation_id'], source_approval),
                                 (capture_operation_id, capture_approval)):
        record = operations._find_operation(report, identifier)
        record['state'] = 'consumed'
        record['audit']['started'] = {
            'at': stamp, 'approved_by': approval['approved_by'], 'reference': approval['reference'],
        }
    persisted = store.commit(report, expected_revision)
    return dict(checked, revision=persisted['revision'], consumed=True)
