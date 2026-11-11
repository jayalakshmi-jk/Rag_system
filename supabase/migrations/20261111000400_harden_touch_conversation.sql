-- Pin the search_path of the trigger function (Supabase security advisor: function_search_path_mutable).
alter function public.touch_conversation() set search_path = public;
