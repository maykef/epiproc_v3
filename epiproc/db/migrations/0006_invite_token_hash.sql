-- Invite tokens are now stored HASHED (sha256 hex), never in cleartext: a DB
-- dump or backup leaking must not hand an attacker a usable password-set link.
-- Rename the column to reflect that it holds a hash, and neutralise any existing
-- cleartext tokens (they can no longer be matched anyway once lookups hash the
-- incoming value; marking them used stops the plaintext rows being usable).
ALTER TABLE invite_tokens RENAME COLUMN token TO token_hash;
UPDATE invite_tokens SET used = TRUE WHERE used = FALSE;
