import app

# happy-path interval growth on "good" (q=4)
i, r, e = app.sm2(0, 0, 2.5, 4);  assert (i, r) == (1, 1), (i, r)          # first success -> 1 day
i, r, e = app.sm2(1, 1, 2.5, 4);  assert (i, r) == (6, 2), (i, r)          # second -> 6 days
i, r, e = app.sm2(6, 2, 2.5, 4);  assert (i, r) == (15, 3), (i, r)         # third -> round(6*2.5)
assert abs(e - 2.5) < 1e-9, e                                              # q=4 leaves ease unchanged

# lapse resets reps+interval and lowers ease
i, r, e = app.sm2(15, 3, 2.5, 0)
assert (i, r) == (1, 0), (i, r)
assert abs(e - 1.7) < 1e-9, e                                             # 2.5 + (0.1 - 5*(0.08+5*0.02)) = 1.7

# ease never drops below 1.3
i, r, e = app.sm2(1, 0, 1.3, 0);  assert e == 1.3, e

print("test_cards OK")
