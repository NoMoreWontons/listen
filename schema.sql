-- Run once in the Supabase SQL editor.
create table recordings (
  id uuid primary key default gen_random_uuid(),
  title text,
  created_at timestamptz default now(),
  status text not null default 'recording',  -- recording | transcribing | done | error
  transcript text,
  summary text,
  stage text,        -- loading_model | transcribing | summarizing, null when not in progress
  progress int,       -- 0-100, null when indeterminate (e.g. model still downloading)
  tokens_in int,       -- Claude summary call: input tokens
  tokens_out int,      -- Claude summary call: output tokens
  semester text,       -- college-lecture org top level, e.g. 'Fall 26' (Obsidian folder)
  class text,          -- course subject (Obsidian subfolder)
  unit text,           -- unit/module within the class (Obsidian subfolder)
  topic text,          -- specific lecture topic (Obsidian note title)
  obsidian_path text,  -- absolute path of the written .md note, null until filed
  source text default 'local',  -- 'local' (whisper) or 'notion' (AI meeting notes)
  notion_id text       -- source Notion page id, dedup key for the poller
);
create unique index if not exists recordings_notion_id_key
  on recordings (notion_id) where notion_id is not null;

-- Migration for an existing table (safe to re-run):
-- alter table recordings
--   add column if not exists stage text,
--   add column if not exists progress int,
--   add column if not exists tokens_in int,
--   add column if not exists tokens_out int,
--   add column if not exists semester text,
--   add column if not exists class text,
--   add column if not exists unit text,
--   add column if not exists topic text,
--   add column if not exists obsidian_path text,
--   add column if not exists source text default 'local',
--   add column if not exists notion_id text;
