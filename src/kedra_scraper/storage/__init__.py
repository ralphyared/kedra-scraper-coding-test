"""Persistence adapters: object storage for documents, MongoDB for metadata.

The split is the core of the design. Object stores are cheap and good at large
immutable blobs but cannot answer questions about them; databases answer
questions but handle large binaries badly. So the bytes live in S3-compatible
storage and the queryable facts live in Mongo, each metadata record carrying a
pointer to its object.
"""
