-- RAG Document Chat: core schema.
-- Embedding dimension: vector(1024) matches Voyage `voyage-4` default output (EMBED_DIM=1024).
-- Changing EMBED_DIM later needs a new migration (alter column + rebuild index) and
-- `python -m scripts.reindex`.

create extension if not exists vector;

-- ------------------------------------------------------------------ profiles
create table profiles (
  id uuid primary key references auth.users(id) on delete cascade,
  name text,
  created_at timestamptz not null default now()
);

create function handle_new_user() returns trigger
language plpgsql security definer set search_path = public as $$
begin
  insert into profiles (id, name)
  values (
    new.id,
    coalesce(
      new.raw_user_meta_data->>'name',
      new.raw_user_meta_data->>'full_name'
    )
  )
  on conflict (id) do nothing;
  return new;
end $$;

create trigger on_auth_user_created
after insert on auth.users
for each row execute function handle_new_user();

-- ----------------------------------------------------------------- documents
create table documents (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  title text not null,
  file_name text,
  status text not null default 'processing'
    check (status in ('processing', 'ready', 'failed')),
  error text,
  page_count int,
  -- Operational fields
  file_size bigint,                       -- shown in the UI, audited against MAX_UPLOAD_MB
  mime_type text,                         -- validated content type
  file_hash text,                         -- SHA-256, per-user duplicate detection
  word_count int,                         -- document size indicator
  processing_stage text not null default 'uploading'
    check (processing_stage in
      ('uploading', 'parsing', 'chunking', 'embedding', 'indexing', 'ready', 'failed')),
  processing_progress smallint not null default 0
    check (processing_progress between 0 and 100),
  processing_started_at timestamptz not null default now(),
  processing_completed_at timestamptz,
  summary jsonb,                          -- cached grounded summary
  created_at timestamptz not null default now()
);

create index documents_user_idx on documents(user_id, created_at desc);
create index documents_hash_idx on documents(user_id, file_hash);
create index documents_stale_idx on documents(processing_started_at) where status = 'processing';

-- Page text kept separately from overlapping chunks so the source panel can show real pages.
create table document_pages (
  document_id uuid not null references documents(id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade,
  page_no int not null,
  content text not null,
  primary key (document_id, page_no)
);

create index document_pages_user_idx on document_pages(user_id);

-- ------------------------------------------------------------------- chunks
create table chunks (
  id uuid primary key default gen_random_uuid(),
  document_id uuid not null references documents(id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade,
  chunk_index int not null,
  page_no int,
  section_title text,
  token_count int,
  content text not null,
  embedding vector(1024),
  tsv tsvector generated always as (to_tsvector('simple', content)) stored
);

create index chunks_user_idx on chunks(user_id, document_id);
create index chunks_doc_idx on chunks(document_id, chunk_index);
create index chunks_tsv_idx on chunks using gin (tsv);
create index chunks_emb_idx on chunks using hnsw (embedding vector_cosine_ops);

-- ------------------------------------------------------------ conversations
create table conversations (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  title text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index conversations_user_idx on conversations(user_id, updated_at desc);

create table messages (
  id uuid primary key default gen_random_uuid(),
  conversation_id uuid not null references conversations(id) on delete cascade,
  role text not null check (role in ('user', 'assistant')),
  content text not null,
  sources jsonb,
  created_at timestamptz not null default now()
);

create index messages_conv_idx on messages(conversation_id, created_at);

create table message_feedback (
  id uuid primary key default gen_random_uuid(),
  message_id uuid not null references messages(id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade,
  rating text not null check (rating in ('up', 'down')),
  reason text check (reason in (
    'incorrect_answer', 'wrong_citation', 'missing_information',
    'too_long', 'didnt_understand', 'other')),
  created_at timestamptz not null default now(),
  unique (message_id, user_id)
);

create index message_feedback_user_idx on message_feedback(user_id);

-- -------------------------------------------------------------------- usage
create table usage_daily (
  user_id uuid not null references auth.users(id) on delete cascade,
  day date not null default current_date,
  questions int not null default 0,
  primary key (user_id, day)
);
