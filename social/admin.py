from django.contrib import admin

from .models import (
    Block, Comment, CommentLike, Follow, Like, Message, Notification,
    Post, Profile, Report, Sticker, Story,
)

admin.site.register(Profile)
admin.site.register(Follow)
admin.site.register(Post)
admin.site.register(Like)
admin.site.register(Comment)
admin.site.register(CommentLike)
admin.site.register(Story)
admin.site.register(Message)
admin.site.register(Sticker)
admin.site.register(Notification)
admin.site.register(Block)
admin.site.register(Report)
