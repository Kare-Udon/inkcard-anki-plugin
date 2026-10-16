InkCards stores runtime data in this add-on's user_files directory, isolated by
Anki profile.

The add-on creates profile-specific directories under user_files/profiles/ and
SQLite records for received review events and card snapshots. Do not edit these
files manually, copy them to public locations, or commit device credentials to
version control. Use the add-on's diagnostic and export tools before cleanup.

This data is separate from the Anki collection and is not synced through AnkiWeb.
