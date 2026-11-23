"""Server-side face verification: compare a user's selfie with their ID photo.

The mobile app only detects and crops faces as a usability pre-check. The
match itself, the attempt limit and the write to users/{uid}.faceVerified
all happen here, so no matching logic or credentials ship in the app.
"""
