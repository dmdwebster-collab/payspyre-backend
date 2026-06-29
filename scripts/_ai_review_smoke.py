"""Throwaway file to verify the cross-model AI reviewer fires on PRs. Safe to delete."""


def collect(item, bucket=[]):
    # Intentional smell for the AI reviewer to catch: mutable default argument
    # means `bucket` is shared across calls and accumulates state between them.
    bucket.append(item)
    return bucket
