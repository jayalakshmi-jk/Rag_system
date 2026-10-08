-- Row Level Security on every table.
--
-- Design: the browser only uses supabase-js for sign-in. All reads and writes of application
-- data go through the FastAPI server using the service role (which bypasses RLS) and explicit
-- user_id filters. RLS is the second line of defence for any direct PostgREST access made with
-- a user's JWT: users may only READ their own rows, and may not write anything. In particular a
-- user can never edit usage_daily to reset their quota or flip a document's status.

alter table profiles enable row level security;
alter table documents enable row level security;
alter table document_pages enable row level security;
alter table chunks enable row level security;
alter table conversations enable row level security;
alter table messages enable row level security;
alter table message_feedback enable row level security;
alter table usage_daily enable row level security;

create policy profiles_select_own on profiles
  for select to authenticated using (id = auth.uid());

create policy documents_select_own on documents
  for select to authenticated using (user_id = auth.uid());

create policy document_pages_select_own on document_pages
  for select to authenticated using (user_id = auth.uid());

create policy chunks_select_own on chunks
  for select to authenticated using (user_id = auth.uid());

create policy conversations_select_own on conversations
  for select to authenticated using (user_id = auth.uid());

-- Messages are readable only when the owning conversation belongs to the caller.
create policy messages_select_own on messages
  for select to authenticated using (
    exists (
      select 1 from conversations c
      where c.id = messages.conversation_id and c.user_id = auth.uid()
    )
  );

create policy message_feedback_select_own on message_feedback
  for select to authenticated using (user_id = auth.uid());

create policy usage_daily_select_own on usage_daily
  for select to authenticated using (user_id = auth.uid());

-- Table privileges: Supabase grants broad defaults to anon/authenticated. Remove them.
revoke all on table
  profiles, documents, document_pages, chunks, conversations, messages, message_feedback,
  usage_daily
from anon, authenticated;

grant select on table
  profiles, documents, document_pages, chunks, conversations, messages, message_feedback,
  usage_daily
to authenticated;

grant all on table
  profiles, documents, document_pages, chunks, conversations, messages, message_feedback,
  usage_daily
to service_role;
