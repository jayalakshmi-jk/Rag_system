-- Database functions. SECURITY DEFINER functions take a user id as a parameter, so they are
-- only ever executable by the server (service_role): see the grants at the end of this file.

-- Atomically consume one question from today's quota. Returns false when the limit is reached.
create function consume_question(p_user uuid, p_limit int) returns boolean
language plpgsql security definer set search_path = public as $$
declare
  n int;
begin
  insert into usage_daily(user_id, day, questions)
  values (p_user, current_date, 0)
  on conflict do nothing;

  update usage_daily
  set questions = questions + 1
  where user_id = p_user
    and day = current_date
    and questions < p_limit
  returning questions into n;

  return n is not null;
end $$;

-- Give back a question when a request failed before the model produced an answer.
create function refund_question(p_user uuid) returns void
language sql security definer set search_path = public as $$
  update usage_daily
  set questions = greatest(questions - 1, 0)
  where user_id = p_user and day = current_date;
$$;

-- Hybrid retrieval: top `candidates` by vector cosine + top `candidates` by full text,
-- fused with reciprocal rank fusion (1 / (60 + rank)). Always restricted to the given user and
-- to that user's READY documents (optionally limited to doc_ids).
create function hybrid_search(
  q_text text,
  q_emb vector(1024),
  p_user uuid,
  doc_ids uuid[] default null,
  k int default 20,
  candidates int default 20
) returns table (
  chunk_id uuid,
  document_id uuid,
  title text,
  page_no int,
  section_title text,
  chunk_index int,
  content text,
  score double precision
)
language sql stable set search_path = public, extensions as $$
  with eligible as (
    select d.id, d.title
    from documents d
    where d.user_id = p_user
      and d.status = 'ready'
      and (doc_ids is null or d.id = any(doc_ids))
  ),
  sem as (
    select c.id,
           row_number() over (order by c.embedding <=> q_emb) as rnk
    from chunks c
    where c.user_id = p_user
      and c.embedding is not null
      and c.document_id in (select id from eligible)
    order by c.embedding <=> q_emb
    limit candidates
  ),
  ft as (
    select c.id,
           row_number() over (order by ts_rank_cd(c.tsv, q.query) desc, c.id) as rnk
    from chunks c
    cross join websearch_to_tsquery('simple', coalesce(q_text, '')) as q(query)
    where c.user_id = p_user
      and c.document_id in (select id from eligible)
      and c.tsv @@ q.query
    order by ts_rank_cd(c.tsv, q.query) desc, c.id
    limit candidates
  ),
  fused as (
    select u.id, sum(1.0 / (60 + u.rnk))::double precision as score
    from (
      select id, rnk from sem
      union all
      select id, rnk from ft
    ) u
    group by u.id
  )
  select c.id, c.document_id, e.title, c.page_no, c.section_title, c.chunk_index, c.content,
         f.score
  from fused f
  join chunks c on c.id = f.id
  join eligible e on e.id = c.document_id
  order by f.score desc, c.document_id, c.chunk_index
  limit k
$$;

-- Conversation search by title or message text. LIKE wildcards in the query are escaped.
create function search_conversations(p_user uuid, q text, max_rows int default 50)
returns setof conversations
language sql stable set search_path = public as $$
  select c.*
  from conversations c
  where c.user_id = p_user
    and (
      c.title ilike '%' || replace(replace(replace(q, '\', '\\'), '%', '\%'), '_', '\_') || '%'
      or exists (
        select 1 from messages m
        where m.conversation_id = c.id
          and m.content ilike
              '%' || replace(replace(replace(q, '\', '\\'), '%', '\%'), '_', '\_') || '%'
      )
    )
  order by c.updated_at desc
  limit max_rows
$$;

-- Keep conversations ordered by last activity.
create function touch_conversation() returns trigger
language plpgsql as $$
begin
  update conversations set updated_at = now() where id = new.conversation_id;
  return new;
end $$;

create trigger messages_touch_conversation
after insert on messages
for each row execute function touch_conversation();

-- ---------------------------------------------------------------- privileges
-- Functions are executable by PUBLIC by default. Lock them down: only the server may call them.
revoke execute on function handle_new_user() from public, anon, authenticated;
revoke execute on function consume_question(uuid, int) from public, anon, authenticated;
revoke execute on function refund_question(uuid) from public, anon, authenticated;
revoke execute on function hybrid_search(text, vector, uuid, uuid[], int, int)
  from public, anon, authenticated;
revoke execute on function search_conversations(uuid, text, int)
  from public, anon, authenticated;
revoke execute on function touch_conversation() from public, anon, authenticated;

grant execute on function consume_question(uuid, int) to service_role;
grant execute on function refund_question(uuid) to service_role;
grant execute on function hybrid_search(text, vector, uuid, uuid[], int, int) to service_role;
grant execute on function search_conversations(uuid, text, int) to service_role;
