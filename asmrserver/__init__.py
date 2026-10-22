"""API FastAPI cho asmrcore: nhận file, chạy job nền, báo tiến độ, trả kết quả.

Mọi trạng thái nằm trên đĩa (thư mục job + job.json), nên cùng một code chạy được với
ThreadRunner (máy cá nhân) và RqRunner (server, worker ở tiến trình khác).
"""
