-- Run once in the Supabase SQL editor.
create table recordings (
  id uuid primary key default gen_random_uuid(),
  title text,
  created_at timestamptz default now(),
  status text not null default 'recording',  -- recording | transcribing | done | error
  transcript text,
  summary text
);
