"""The curated zone: landing data made useful without being made lossy.

The landing zone holds exactly what the site served, and is never modified. This
package derives a second representation from it -- page furniture stripped,
documents renamed predictably, a few fields lifted out of the prose -- and writes
that to a separate bucket and collection.

Keeping the two apart is what makes the transformation safe to change. If the
cleaning rules turn out to be wrong, the curated zone can be rebuilt from the
landing zone; nothing that was scraped is ever lost to a parsing decision made
afterwards.
"""
