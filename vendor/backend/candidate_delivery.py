"""Explicit publication-to-package step; downloadability is not acceptance.

Like a deterministic Flow step, this consumes declared immutable inputs. It
never scans workspaces, invents a product entrypoint or invokes another model.
"""
from .delivery import DeliveryError, create_delivery, validate_delivery


def candidate_enabled(task):
    contract = task.get('outputContract') or {}
    return contract.get('kind') == 'artifact' and contract.get('candidateDelivery') is True


def create_candidate_delivery(run, task, store, artifact_ids, entrypoint, instructions):
    if not candidate_enabled(task):
        raise DeliveryError('Candidate packaging requires this producer\'s explicit artifact candidateDelivery contract.')
    delivery = create_delivery(run, task, store, artifact_ids, entrypoint, instructions)
    entry = next(item for item in delivery['files'] if item['path'] == delivery['entrypoint'])
    if (entry['sourceTaskId'] != task.get('id') or entry['artifactId'] not in task.get('outputs', [])
            or entry['inherited'] or entry['sourceRevision'] != task.get('revision', 0)):
        raise DeliveryError('The candidate entrypoint must be this producer\'s new current published output, not inherited work plus a new README.')
    delivery.update(status='candidate', reviewStatusAtCreation='pending', semanticQuality='unassessed',
                    purpose='Published product candidate; independent review and formal delivery remain separate.')
    return delivery


def publication_candidate(run, task, store, published, declaration):
    if (not isinstance(declaration, dict)
            or set(declaration) - {'entrypoint', 'instructions', 'artifactIds'}):
        raise DeliveryError('Publication delivery needs explicit entrypoint/instructions and optional existing artifactIds.')
    extra = declaration.get('artifactIds', [])
    if not isinstance(extra, list) or any(not isinstance(item, str) for item in extra):
        raise DeliveryError('Publication delivery artifactIds must be explicit already published IDs.')
    ids = [item['id'] for item in published] + extra
    # Repeating a current ID in the batch plus explicit assets is harmless;
    # different IDs at a colliding path still fail the original package checks.
    ids = list(dict.fromkeys(ids))
    return create_candidate_delivery(run, task, store, ids,
                                     declaration.get('entrypoint'), declaration.get('instructions'))


def current_candidate(task, run, store, eligible_ids, minimum=1):
    for delivery in reversed(run.get('deliveries', [])):
        if (delivery.get('status') != 'candidate' or delivery.get('taskId') != task.get('id')
                or delivery.get('agentId') != task.get('agentId')):
            continue
        try:
            snapshots = validate_delivery(delivery, run, store, current=True)
        except (ValueError, KeyError, TypeError):
            continue
        # Preserve the producer's original minCount/format requirement without
        # forcing ancillary published notes into the explicit product selection.
        if len(set(eligible_ids).intersection(item['id'] for item in snapshots)) >= minimum:
            return delivery
    return None


def existing_candidate(run, store, proposed):
    """Reuse this exact current declaration without pretending to execute QA."""
    identity = ('runId', 'taskId', 'taskRevision', 'agentId', 'entrypoint', 'instructions')
    files = sorted(proposed['files'], key=lambda item: item['path'])
    for previous in reversed(run.get('deliveries', [])):
        if (previous.get('status') != 'candidate'
                or any(previous.get(key) != proposed.get(key) for key in identity)
                or sorted(previous.get('files', []), key=lambda item: item['path']) != files):
            continue
        try:
            validate_delivery(previous, run, store, current=True)
        except (ValueError, KeyError, TypeError):
            continue
        return previous
    return None
