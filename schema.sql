-- Run once in the Supabase SQL editor.
create table recordings (
  id uuid primary key default gen_random_uuid(),
  title text,
  created_at timestamptz default now(),
  status text not null default 'recording',  -- recording | transcribing | done | error | split_pending
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
  notion_id text,      -- source Notion page id, dedup key for the poller
  notes text,          -- user's own notes, integrated into the Claude summary
  pending_segments jsonb  -- proposed [{class,unit,topic,summary}, ...] while status='split_pending'
);
create unique index if not exists recordings_notion_id_key
  on recordings (notion_id) where notion_id is not null;

-- source also takes 'upload_audio' | 'pdf' | 'syllabus' | 'homework' | 'youtube' for uploads.

-- Syllabus due dates. gcal_event_id unused until Calendar API sync (Approach B).
create table if not exists assignments (
  id uuid primary key default gen_random_uuid(),
  recording_id uuid references recordings(id) on delete cascade,
  title text not null,
  due_on date not null,      -- date-only: avoids timezone day-shift
  klass text default '',
  kind text default 'assignment',  -- assignment | exam | quiz | project
  gcal_event_id text,
  created_at timestamptz default now(),
  unique (klass, title)      -- re-upload upserts instead of duplicating
);

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
--   add column if not exists notion_id text,
--   add column if not exists notes text;

-- Homework uploads link to a due date; quizzes store generated practice sets.
alter table assignments add column if not exists status text default 'open';       -- open | submitted
alter table assignments add column if not exists homework_id uuid references recordings(id) on delete set null;

create table if not exists quizzes (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz default now(),
  kind text default 'quiz',              -- quiz (10 q) | test (20 q)
  semester text,
  class text not null,
  unit text,                             -- null = whole class
  questions jsonb not null,              -- [{type:'mcq'|'short', q, choices?, answer, explanation}]
  answers jsonb,                         -- graded per-question results, null until submitted
  score numeric                          -- 0-100, null until submitted
);

-- Flashcards with SM-2 spaced-repetition scheduling.
create table if not exists cards (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz default now(),
  semester text,
  class text not null,
  unit text,                              -- null = whole class
  front text not null,                    -- prompt (term / question)
  back text not null,                     -- answer
  due_at timestamptz default now(),       -- when next due
  interval int default 0,                 -- days to next review
  reps int default 0,                     -- consecutive successful reviews
  ease numeric default 2.5                -- SM-2 ease factor (floor 1.3)
);
alter table cards enable row level security;   -- match quizzes/recordings; app uses the service key

-- Multi-topic split: a lecture that covers several distinct topics proposes
-- one segment per topic and waits (status='split_pending') for the user to
-- approve/decline via POST /split before filing.
alter table recordings add column if not exists pending_segments jsonb;
alter table recordings add column if not exists addendum text;  -- post-filing corrections/additions, rendered verbatim in the note
