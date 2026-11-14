from datetime import timedelta
from pathlib import Path
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone


def story_expiry():
    return timezone.now() + timedelta(hours=24)


class Profile(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='profile'
    )
    avatar = models.ImageField(upload_to='avatars/', blank=True)
    bio = models.CharField(max_length=160, blank=True)
    website = models.URLField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'@{self.user.username}'

    @property
    def follower_count(self):
        return self.user.followers.count()

    @property
    def following_count(self):
        return self.user.following.count()


class Follow(models.Model):
    follower = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='following'
    )
    followed = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='followers'
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=('follower', 'followed'), name='unique_follow'),
            models.CheckConstraint(
                condition=~Q(follower=models.F('followed')), name='cannot_follow_self'
            ),
        ]
        ordering = ('-created_at',)


class Post(models.Model):
    class MediaType(models.TextChoices):
        IMAGE = 'image', 'Photo'
        VIDEO = 'video', 'Video'

    class MediaFilter(models.TextChoices):
        ORIGINAL = 'original', 'Original'
        WARM = 'warm', 'Warm'
        SOFT = 'soft', 'Soft'
        MONO = 'mono', 'Mono'
        BRIGHT = 'bright', 'Bright'

    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='posts'
    )
    media = models.FileField(upload_to='posts/')
    media_type = models.CharField(max_length=8, choices=MediaType.choices, default=MediaType.IMAGE)
    media_filter = models.CharField(
        max_length=8, choices=MediaFilter.choices, default=MediaFilter.ORIGINAL
    )
    caption = models.CharField(max_length=2200, blank=True)
    location = models.CharField(max_length=120, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ('-created_at',)

    def __str__(self):
        return f'Post by @{self.author.username}'

    @property
    def like_count(self):
        return self.likes.count()

    @property
    def comment_count(self):
        return self.comments.count()

    @property
    def top_level_comments(self):
        return self.comments.filter(parent__isnull=True).select_related('author')[:3]


class Like(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='likes'
    )
    post = models.ForeignKey(Post, on_delete=models.CASCADE, related_name='likes')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=('user', 'post'), name='unique_post_like')
        ]


class Comment(models.Model):
    post = models.ForeignKey(Post, on_delete=models.CASCADE, related_name='comments')
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='comments'
    )
    parent = models.ForeignKey(
        'self', on_delete=models.CASCADE, related_name='replies', null=True, blank=True
    )
    body = models.CharField(max_length=1000)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ('created_at',)


class CommentLike(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='comment_likes'
    )
    comment = models.ForeignKey(Comment, on_delete=models.CASCADE, related_name='likes')

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=('user', 'comment'), name='unique_comment_like')
        ]


class Story(models.Model):
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='stories'
    )
    media = models.FileField(upload_to='stories/')
    media_type = models.CharField(max_length=8, choices=Post.MediaType.choices, default='image')
    caption = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(default=story_expiry, db_index=True)

    class Meta:
        ordering = ('-created_at',)

    @property
    def is_active(self):
        return self.expires_at > timezone.now()


class Sticker(models.Model):
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='stickers'
    )
    image = models.ImageField(upload_to='stickers/')
    label = models.CharField(max_length=60, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ('-created_at',)


class Message(models.Model):
    sender = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='sent_messages'
    )
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='received_messages'
    )
    body = models.CharField(max_length=2000, blank=True)
    attachment = models.FileField(upload_to='chat/', blank=True)
    sticker = models.ForeignKey(
        Sticker, on_delete=models.SET_NULL, null=True, blank=True, related_name='messages'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ('created_at',)

    def clean(self):
        if self.sender_id and self.sender_id == self.recipient_id:
            raise ValidationError('You cannot message yourself.')
        if not self.body.strip() and not self.attachment and not self.sticker_id:
            raise ValidationError('A message needs text, a file, or a sticker.')
        if self.sticker_id and self.sender_id and self.sticker.owner_id != self.sender_id:
            raise ValidationError('You can only send a sticker from your own collection.')

    @property
    def attachment_is_image(self):
        return Path(self.attachment.name).suffix.lower() in {
            '.jpg', '.jpeg', '.png', '.webp', '.gif'
        }

    @property
    def attachment_is_video(self):
        return Path(self.attachment.name).suffix.lower() in {'.mp4', '.webm'}


class BlobUploadTicket(models.Model):
    class Purpose(models.TextChoices):
        AVATAR = 'avatar', 'Profile photo'
        POST = 'post', 'Post'
        STORY = 'story', 'Story'
        CHAT = 'chat', 'Chat attachment'
        STICKER = 'sticker', 'Sticker'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='blob_upload_tickets'
    )
    purpose = models.CharField(max_length=8, choices=Purpose.choices)
    pathname = models.CharField(max_length=950, unique=True)
    original_name = models.CharField(max_length=255)
    content_type = models.CharField(max_length=100)
    size = models.PositiveBigIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    consumed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ('-created_at',)


class Call(models.Model):
    class Kind(models.TextChoices):
        AUDIO = 'audio', 'Audio'
        VIDEO = 'video', 'Video'

    class Status(models.TextChoices):
        RINGING = 'ringing', 'Ringing'
        ACTIVE = 'active', 'Active'
        DECLINED = 'declined', 'Declined'
        ENDED = 'ended', 'Ended'
        MISSED = 'missed', 'Missed'

    caller = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='calls_started'
    )
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='calls_received'
    )
    kind = models.CharField(max_length=8, choices=Kind.choices)
    status = models.CharField(
        max_length=8, choices=Status.choices, default=Status.RINGING, db_index=True
    )
    offer = models.JSONField()
    answer = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    answered_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ('-created_at',)


class CallSignal(models.Model):
    call = models.ForeignKey(Call, on_delete=models.CASCADE, related_name='signals')
    sender = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    candidate = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ('pk',)


class Notification(models.Model):
    class Kind(models.TextChoices):
        LIKE = 'like', 'Like'
        COMMENT_LIKE = 'comment_like', 'Comment like'
        COMMENT = 'comment', 'Comment'
        FOLLOW = 'follow', 'Follow'
        MESSAGE = 'message', 'Message'

    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='notifications'
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='activity'
    )
    kind = models.CharField(max_length=16, choices=Kind.choices)
    post = models.ForeignKey(Post, on_delete=models.CASCADE, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    is_read = models.BooleanField(default=False)

    class Meta:
        ordering = ('-created_at',)


class Block(models.Model):
    blocker = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='blocks_made'
    )
    blocked = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='blocks_received'
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=('blocker', 'blocked'), name='unique_user_block'),
            models.CheckConstraint(
                condition=~Q(blocker=models.F('blocked')), name='cannot_block_self'
            ),
        ]


class Report(models.Model):
    class Reason(models.TextChoices):
        SPAM = 'spam', 'Spam'
        ABUSE = 'abuse', 'Harassment or abuse'
        INAPPROPRIATE = 'inappropriate', 'Inappropriate content'
        OTHER = 'other', 'Other'

    reporter = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='reports'
    )
    reported_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True,
        related_name='reports_against'
    )
    post = models.ForeignKey(Post, on_delete=models.CASCADE, null=True, blank=True)
    reason = models.CharField(max_length=20, choices=Reason.choices)
    details = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def clean(self):
        if self.reported_user_id and self.post_id:
            raise ValidationError('Report exactly one user or post.')

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(reported_user__isnull=False, post__isnull=True)
                    | Q(reported_user__isnull=True, post__isnull=False)
                ),
                name='report_exactly_one_target',
            )
        ]
