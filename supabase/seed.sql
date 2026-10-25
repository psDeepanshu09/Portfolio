-- Sample data. Edit services, prices and hours to match the real salon.
insert into public.services (name, description, price, duration_minutes) values
  ('Haircut and style', 'Wash, cut and blow-dry.',               600, 45),
  ('Colour',            'Root touch-up, global colour or highlights.', 2500, 120),
  ('Hair spa',          'Deep conditioning treatment and head massage.', 1000, 60);

-- 0 = Sunday ... 6 = Saturday
insert into public.opening_hours (weekday, is_open, open_time, close_time) values
  (0, true, '11:00', '18:00'),
  (1, true, '10:00', '20:00'), (2, true, '10:00', '20:00'), (3, true, '10:00', '20:00'),
  (4, true, '10:00', '20:00'), (5, true, '10:00', '20:00'),
  (6, true, '09:00', '21:00');

insert into public.breaks (weekday, start_time, end_time, label) values
  (null, '14:00', '14:30', 'Lunch break');
