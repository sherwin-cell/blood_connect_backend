"""Private uploads: ID photos and Blood Request Forms.

Stored on Cloudinary as type=private (no public URL) under
<private folder>/<uid>/<kind>/. Firestore keeps an opaque reference
("secure://...") instead of a link; the owner or a PRC admin exchanges it
for a short-lived signed URL at GET /api/files/view.
"""
