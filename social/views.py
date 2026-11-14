import json
import logging
import mimetypes
from pathlib import PurePosixPath
from datetime import timedelta
from urllib.error import HTTPError, URLError

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import PasswordChangeView
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import transaction
from django.db.models import Exists, Max, OuterRef, Q
from django.core.paginator import Paginator
from django.core.exceptions import ValidationError
from django.core.files.storage import default_storage
from django.http import Http404, HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.urls import reverse_lazy
from django.utils import timezone
from django.utils.http import content_disposition_header, url_has_allowed_host_and_scheme
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_POST

from .blob_uploads import (
    create_upload_ticket, decode_ticket_token, sign_ticket, verify_uploaded_blob,
)
from .forms import (
    AccountSettingsForm, CommentForm, MessageForm, PostForm, ProfileForm, ReportForm,
    SignUpForm, StickerForm, StoryForm,
)
from .models import (
    BlobUploadTicket, Block, Call, CallSignal, Comment, CommentLike, Follow, Like, Message,
    Notification, Post, Profile, Story, Sticker,
)
from .storage import BlobStorageError

User = get_user_model()
logger = logging.getLogger(__name__)


def blocked_user_ids(user):
    return Block.objects.filter(Q(blocker=user) | Q(blocked=user)).values_list(
        'blocker_id', 'blocked_id'
    )


@login_required
@require_POST
def blob_upload_token(request):
    if not settings.BLOB_UPLOAD_ENABLED:
        return JsonResponse({'error': 'Persistent uploads are not configured.'}, status=503)
    try:
        payload = json.loads(request.body)
        ticket = create_upload_ticket(
            request.user,
            payload.get('purpose'),
            payload.get('name', ''),
            payload.get('size'),
        )
    except (json.JSONDecodeError, UnicodeDecodeError, AttributeError, TypeError):
        return JsonResponse({'error': 'The upload request is invalid.'}, status=400)
    except ValidationError as error:
        return JsonResponse({'error': error.messages[0]}, status=400)
    return JsonResponse({
        'pathname': ticket.pathname,
        'content_type': ticket.content_type,
        'token': sign_ticket(ticket),
    })


@csrf_exempt
@require_POST
def blob_upload_complete(request):
    if not settings.BLOB_UPLOAD_ENABLED:
        return JsonResponse({'error': 'Persistent uploads are not configured.'}, status=503)
    try:
        data = json.loads(request.body)
        token = data['token']
        pathname = data['pathname']
        payload = decode_ticket_token(token)
    except (json.JSONDecodeError, UnicodeDecodeError, KeyError, TypeError, ValidationError):
        return JsonResponse({'error': 'The upload completion request is invalid.'}, status=400)
    if payload.get('path') != pathname:
        return JsonResponse({'error': 'The upload path does not match its authorization.'}, status=400)
    ticket = BlobUploadTicket.objects.filter(
        pk=payload.get('id'),
        owner_id=payload.get('user'),
        purpose=payload.get('purpose'),
        pathname=pathname,
        expires_at__gt=timezone.now(),
    ).first()
    if not ticket or ticket.consumed_at:
        return JsonResponse({'error': 'The upload authorization is expired or already used.'}, status=409)
    if ticket.completed_at:
        return JsonResponse({'ok': True})
    try:
        verify_uploaded_blob(ticket)
    except ValidationError as error:
        return JsonResponse({'error': error.messages[0]}, status=400)
    ticket.completed_at = timezone.now()
    ticket.save(update_fields=('completed_at',))
    return JsonResponse({'ok': True})


@login_required
@require_http_methods(['GET', 'HEAD'])
def private_media(request, path):
    if not settings.BLOB_UPLOAD_ENABLED:
        raise Http404
    hidden_ids = set(hidden_user_ids(request.user))
    if path.startswith('uploads/avatar/'):
        allowed = Profile.objects.filter(avatar=path).exclude(user_id__in=hidden_ids).exists()
    elif path.startswith('uploads/post/'):
        allowed = Post.objects.filter(media=path).exclude(author_id__in=hidden_ids).exists()
    elif path.startswith('uploads/story/'):
        allowed = Story.objects.filter(
            media=path, expires_at__gt=timezone.now()
        ).exclude(author_id__in=hidden_ids).exists()
    elif path.startswith('uploads/chat/'):
        allowed = Message.objects.filter(attachment=path).filter(
            Q(sender=request.user) | Q(recipient=request.user)
        ).exclude(sender_id__in=hidden_ids).exclude(recipient_id__in=hidden_ids).exists()
    elif path.startswith('uploads/sticker/'):
        allowed = Sticker.objects.filter(image=path).exclude(owner_id__in=hidden_ids).filter(
            Q(owner=request.user)
            | Q(messages__sender=request.user)
            | Q(messages__recipient=request.user)
        ).exists()
    else:
        allowed = False
    if not allowed:
        raise Http404

    try:
        upstream = default_storage.open_response(
            path,
            request.headers.get('Range'),
            method=request.method,
        )
    except HTTPError as error:
        if error.code == 404:
            raise Http404 from error
        if error.code == 416:
            response = HttpResponse(status=416)
            content_range = error.headers.get('Content-Range')
            if content_range:
                response['Content-Range'] = content_range
            return response
        logger.exception('Vercel Blob returned an error while serving private media.')
        return HttpResponse('Media is temporarily unavailable.', status=503)
    except (URLError, TimeoutError, BlobStorageError):
        logger.exception('Could not retrieve private media from Vercel Blob.')
        return HttpResponse('Media is temporarily unavailable.', status=503)

    response = StreamingHttpResponse(
        stream_blob_response(upstream),
        status=upstream.status,
        content_type=mimetypes.guess_type(PurePosixPath(path).name)[0]
        or 'application/octet-stream',
    )
    if upstream.headers.get('Content-Length'):
        response['Content-Length'] = upstream.headers['Content-Length']
    for header in ('Content-Range', 'Accept-Ranges'):
        if upstream.headers.get(header):
            response[header] = upstream.headers[header]
    media_type = response['Content-Type']
    response['Content-Disposition'] = content_disposition_header(
        media_type not in {'image/jpeg', 'image/png', 'image/webp', 'image/gif', 'video/mp4', 'video/webm'},
        PurePosixPath(path).name,
    )
    response['Cache-Control'] = 'private, no-store'
    response['X-Content-Type-Options'] = 'nosniff'
    return response


def stream_blob_response(upstream):
    try:
        while chunk := upstream.read(64 * 1024):
            yield chunk
    finally:
        upstream.close()


def hidden_user_ids(user):
    pairs = blocked_user_ids(user)
    return {user_id for pair in pairs for user_id in pair if user_id != user.pk}


def with_like_state(posts, user):
    return posts.annotate(
        is_liked=Exists(Like.objects.filter(post_id=OuterRef('pk'), user=user))
    )


def safe_return_url(request):
    referer = request.META.get('HTTP_REFERER', '')
    if url_has_allowed_host_and_scheme(
        referer, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return referer
    return reverse('feed')


def is_ajax(request):
    return request.headers.get('x-requested-with') == 'XMLHttpRequest'


def signup(request):
    if request.user.is_authenticated:
        return redirect('feed')
    form = SignUpForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        user = form.save()
        login(request, user)
        return redirect('feed')
    return render(request, 'social/signup.html', {'form': form})


@login_required
def feed(request):
    followed_ids = Follow.objects.filter(follower=request.user).values_list('followed_id', flat=True)
    hidden_ids = hidden_user_ids(request.user)
    posts = Post.objects.filter(Q(author__in=followed_ids) | Q(author=request.user))
    posts = with_like_state(
        posts.exclude(author_id__in=hidden_ids).select_related('author'),
        request.user,
    )
    latest_post_id = posts.aggregate(latest=Max('pk'))['latest'] or 0
    stories = Story.objects.filter(
        author__in=list(followed_ids) + [request.user.pk],
        expires_at__gt=timezone.now(),
    ).exclude(author_id__in=hidden_ids).select_related('author')
    page_obj = Paginator(posts, 8).get_page(request.GET.get('page'))
    return render(
        request, 'social/feed.html',
        {'posts': page_obj, 'page_obj': page_obj, 'stories': stories,
         'hidden_ids': hidden_ids, 'latest_post_id': latest_post_id},
    )


@login_required
def feed_updates(request):
    after_id = request.GET.get('after_id', '0')
    if not after_id.isdigit():
        return JsonResponse({'error': 'Invalid feed cursor.'}, status=400)
    followed_ids = Follow.objects.filter(
        follower=request.user
    ).values_list('followed_id', flat=True)
    posts = Post.objects.filter(
        Q(author__in=followed_ids) | Q(author=request.user),
        pk__gt=int(after_id),
    ).exclude(author_id__in=hidden_user_ids(request.user)).select_related('author')
    posts = with_like_state(posts, request.user).order_by('pk')[:20]
    return JsonResponse({
        'html': render_to_string(
            'social/_feed_update_cards.html',
            {'posts': posts, 'hidden_ids': hidden_user_ids(request.user)},
            request=request,
        ),
        'latest_id': max((post.pk for post in posts), default=int(after_id)),
    })


def explore(request):
    query = request.GET.get('q', '').strip()[:100]
    hidden = hidden_user_ids(request.user) if request.user.is_authenticated else set()
    users = User.objects.exclude(pk__in=hidden)
    posts = Post.objects.exclude(author_id__in=hidden)
    if query:
        users = users.filter(username__icontains=query)
        posts = posts.filter(Q(caption__icontains=query) | Q(location__icontains=query))
    posts = posts.select_related('author')
    if request.user.is_authenticated:
        posts = with_like_state(posts, request.user)
    return render(
        request, 'social/explore.html',
        {'users': users[:10], 'posts': posts[:36], 'query': query},
    )


def profile(request, username):
    person = get_object_or_404(User, username=username)
    if request.user.is_authenticated and person.pk in hidden_user_ids(request.user):
        raise Http404
    is_following = request.user.is_authenticated and Follow.objects.filter(
        follower=request.user, followed=person
    ).exists()
    is_owner = request.user.is_authenticated and request.user.pk == person.pk
    posts = person.posts.all()
    if request.user.is_authenticated:
        posts = with_like_state(posts, request.user)
    return render(
        request, 'social/profile.html',
        {'person': person, 'posts': posts, 'is_following': is_following,
         'is_owner': is_owner},
    )


@login_required
@require_POST
def toggle_follow(request, username):
    person = get_object_or_404(User, username=username)
    if person == request.user or person.pk in hidden_user_ids(request.user):
        raise Http404
    follow, created = Follow.objects.get_or_create(follower=request.user, followed=person)
    if created:
        Notification.objects.create(recipient=person, actor=request.user, kind=Notification.Kind.FOLLOW)
    else:
        follow.delete()
    if is_ajax(request):
        return JsonResponse({
            'following': created,
            'follower_count': person.followers.count(),
        })
    return redirect('profile', username=username)


@login_required
@require_POST
def toggle_like(request, post_id):
    post = get_object_or_404(Post, pk=post_id)
    if post.author_id in hidden_user_ids(request.user):
        raise Http404
    _, created = Like.objects.get_or_create(user=request.user, post=post)
    if not created:
        Like.objects.filter(user=request.user, post=post).delete()
    elif post.author_id != request.user.pk:
        Notification.objects.create(
            recipient=post.author, actor=request.user, kind=Notification.Kind.LIKE, post=post
        )
    if is_ajax(request):
        return JsonResponse({
            'liked': created,
            'like_count': post.likes.count(),
        })
    return redirect(safe_return_url(request))


@login_required
@require_POST
def add_comment(request, post_id):
    post = get_object_or_404(Post, pk=post_id)
    if post.author_id in hidden_user_ids(request.user):
        raise Http404
    form = CommentForm(request.POST)
    if form.is_valid():
        parent_id = request.POST.get('parent_id')
        parent = None
        if parent_id:
            parent = Comment.objects.filter(pk=parent_id, post=post).first()
            if parent is None:
                raise Http404
        comment = form.save(commit=False)
        comment.post = post
        comment.author = request.user
        comment.parent = parent
        comment.save()
        if post.author_id != request.user.pk:
            Notification.objects.create(
                recipient=post.author, actor=request.user,
                kind=Notification.Kind.COMMENT, post=post,
            )
        if is_ajax(request):
            return JsonResponse({
                'html': render_to_string(
                    'social/_comment.html',
                    {'comment': comment, 'hidden_ids': hidden_user_ids(request.user)},
                    request=request,
                ),
                'comment_count': post.comments.count(),
            })
    else:
        if is_ajax(request):
            return JsonResponse({'errors': form.errors.get('body', ['Could not add comment.'])}, status=400)
        messages.error(request, form.errors.get('body', ['Could not add comment.'])[0])
    return redirect(safe_return_url(request))


@login_required
@require_POST
def toggle_comment_like(request, comment_id):
    comment = get_object_or_404(Comment.objects.select_related('post', 'author'), pk=comment_id)
    if comment.author_id in hidden_user_ids(request.user) or comment.post.author_id in hidden_user_ids(request.user):
        raise Http404
    _, created = CommentLike.objects.get_or_create(user=request.user, comment=comment)
    if not created:
        CommentLike.objects.filter(user=request.user, comment=comment).delete()
    elif comment.author_id != request.user.pk:
        Notification.objects.create(
            recipient=comment.author, actor=request.user,
            kind=Notification.Kind.COMMENT_LIKE, post=comment.post,
        )
    if is_ajax(request):
        return JsonResponse({
            'liked': created,
            'like_count': comment.likes.count(),
        })
    return redirect(safe_return_url(request))


@login_required
def create_post(request):
    form = PostForm(request.POST or None, request.FILES or None, user=request.user)
    if request.method == 'POST' and form.is_valid():
        post = form.save(commit=False)
        post.author = request.user
        post.media_type = form.media_type
        form.save_instance(post)
        if is_ajax(request):
            post = with_like_state(
                Post.objects.filter(pk=post.pk).select_related('author'), request.user
            ).get()
            return JsonResponse({
                'message': 'Your post is live.',
                'url': reverse('feed'),
                'html': render_to_string(
                    'social/_post.html',
                    {'post': post, 'hidden_ids': hidden_user_ids(request.user)},
                    request=request,
                ),
            })
        messages.success(request, 'Your post is live.')
        return redirect('feed')
    if request.method == 'POST' and is_ajax(request):
        errors = [
            str(error)
            for error_list in form.errors.values()
            for error in error_list
        ] or ['Could not create the post.']
        return JsonResponse({'errors': errors}, status=400)
    return render(request, 'social/create.html', {'form': form, 'title': 'Create post'})


@login_required
def create_story(request):
    form = StoryForm(request.POST or None, request.FILES or None, user=request.user)
    if request.method == 'POST' and form.is_valid():
        story = form.save(commit=False)
        story.author = request.user
        story.media_type = form.media_type
        form.save_instance(story)
        if is_ajax(request):
            return JsonResponse({'message': 'Your story is live for 24 hours.'})
        messages.success(request, 'Your story is live for 24 hours.')
        return redirect('feed')
    if request.method == 'POST' and is_ajax(request):
        errors = [
            str(error)
            for error_list in form.errors.values()
            for error in error_list
        ] or ['Could not create the story.']
        return JsonResponse({'errors': errors}, status=400)
    return render(request, 'social/create.html', {'form': form, 'title': 'Create story'})


@login_required
def story_detail(request, story_id):
    story = get_object_or_404(
        Story.objects.select_related('author'),
        pk=story_id, expires_at__gt=timezone.now(),
    )
    if story.author_id in hidden_user_ids(request.user):
        raise Http404
    return render(request, 'social/story.html', {'story': story})


@login_required
def reels(request):
    posts = Post.objects.filter(media_type=Post.MediaType.VIDEO).exclude(
        author_id__in=hidden_user_ids(request.user)
    ).select_related('author')
    posts = with_like_state(posts, request.user)
    return render(request, 'social/reels.html', {'posts': posts})


@login_required
def edit_profile(request):
    form = ProfileForm(
        request.POST or None, request.FILES or None,
        instance=request.user.profile, user=request.user,
    )
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, 'Your profile has been updated.')
        return redirect('profile', username=request.user.username)
    return render(request, 'social/edit_profile.html', {'form': form})


@login_required
def account_settings(request):
    form = AccountSettingsForm(request.POST or None, instance=request.user)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, 'Your account details have been updated.')
        return redirect('account_settings')
    return render(request, 'social/account_settings.html', {'form': form})


class SocialPasswordChangeView(PasswordChangeView):
    template_name = 'social/password_change.html'
    success_url = reverse_lazy('account_settings')


@login_required
def activity(request):
    notifications = Notification.objects.filter(
        recipient=request.user
    ).exclude(actor_id__in=hidden_user_ids(request.user)).select_related('actor', 'post')
    notifications.filter(is_read=False).update(is_read=True)
    return render(request, 'social/activity.html', {'notifications': notifications})


@login_required
def activity_updates(request):
    after_id = request.GET.get('after_id')
    if after_id is not None and not after_id.isdigit():
        return JsonResponse({'error': 'Invalid activity cursor.'}, status=400)

    notifications = Notification.objects.filter(
        recipient=request.user
    ).exclude(actor_id__in=hidden_user_ids(request.user))
    if after_id is None:
        latest_id = notifications.aggregate(latest=Max('pk'))['latest'] or 0
        new_notifications = ()
    else:
        new_notifications = tuple(
            notifications.filter(pk__gt=int(after_id)).select_related('actor').order_by('pk')[:20]
        )
        latest_id = new_notifications[-1].pk if new_notifications else int(after_id)

    messages = {
        Notification.Kind.LIKE: 'liked your post',
        Notification.Kind.COMMENT_LIKE: 'liked your comment',
        Notification.Kind.COMMENT: 'commented on your post',
        Notification.Kind.FOLLOW: 'started following you',
        Notification.Kind.MESSAGE: 'sent you a message',
    }
    return JsonResponse({
        'latest_id': latest_id,
        'unread_count': notifications.filter(is_read=False).count(),
        'notifications': [
            {
                'id': notification.pk,
                'actor': notification.actor.username,
                'kind': notification.kind,
                'message': messages[notification.kind],
            }
            for notification in new_notifications
        ],
    })


def call_payload(call):
    return {
        'id': call.pk,
        'kind': call.kind,
        'status': call.status,
        'caller': call.caller.username,
        'recipient': call.recipient.username,
    }


def send_call_event(user_id, payload):
    channel_layer = get_channel_layer()
    async_to_sync(channel_layer.group_send)(
        f'calls_{user_id}',
        {'type': 'call.event', 'payload': payload},
    )


def publish_call_state(call, **extra):
    payload = {'event': 'call_state', 'call': call_payload(call), **extra}
    for user_id in (call.caller_id, call.recipient_id):
        send_call_event(user_id, payload)


@login_required
@require_POST
def start_call(request, username):
    recipient = get_object_or_404(User, username=username)
    if recipient == request.user or recipient.pk in hidden_user_ids(request.user):
        raise Http404

    kind = request.POST.get('kind')
    if kind not in Call.Kind.values:
        return JsonResponse({'error': 'Choose audio or video calling.'}, status=400)
    try:
        offer = json.loads(request.POST.get('offer', ''))
    except (TypeError, ValueError):
        return JsonResponse({'error': 'The call offer is invalid.'}, status=400)
    if (
        not isinstance(offer, dict)
        or offer.get('type') != 'offer'
        or not isinstance(offer.get('sdp'), str)
        or not offer['sdp']
        or len(offer['sdp']) > 50000
    ):
        return JsonResponse({'error': 'The call offer is invalid.'}, status=400)

    active_calls = Call.objects.filter(
        status__in=(Call.Status.RINGING, Call.Status.ACTIVE),
        created_at__gte=timezone.now() - timedelta(hours=2),
    ).filter(
        Q(caller__in=(request.user, recipient)) | Q(recipient__in=(request.user, recipient))
    )
    if active_calls.exists():
        return JsonResponse({'error': 'One of you is already in a call.'}, status=409)

    call = Call.objects.create(
        caller=request.user, recipient=recipient, kind=kind, offer=offer,
    )
    send_call_event(recipient.pk, {
        'event': 'call_offer',
        'call': call_payload(call),
        'offer': call.offer,
    })
    return JsonResponse(call_payload(call), status=201)


@login_required
def incoming_calls(request):
    cutoff = timezone.now() - timedelta(seconds=45)
    Call.objects.filter(
        recipient=request.user, status=Call.Status.RINGING, created_at__lt=cutoff
    ).update(status=Call.Status.MISSED, ended_at=timezone.now())
    calls = Call.objects.filter(
        recipient=request.user,
        status=Call.Status.RINGING,
        created_at__gte=cutoff,
    ).exclude(caller_id__in=hidden_user_ids(request.user)).select_related('caller')[:5]
    return JsonResponse({'calls': [
        {**call_payload(call), 'caller': call.caller.username, 'offer': call.offer}
        for call in calls
    ]})


@login_required
def call_updates(request, call_id):
    call = get_object_or_404(
        Call.objects.select_related('caller', 'recipient').filter(
            Q(caller=request.user) | Q(recipient=request.user)
        ),
        pk=call_id,
    )
    if call.caller_id in hidden_user_ids(request.user):
        raise Http404
    after_id = request.GET.get('after_id', '0')
    if not after_id.isdigit():
        return JsonResponse({'error': 'Invalid call signal cursor.'}, status=400)

    if call.status == Call.Status.RINGING and call.created_at < timezone.now() - timedelta(seconds=45):
        Call.objects.filter(pk=call.pk, status=Call.Status.RINGING).update(
            status=Call.Status.MISSED, ended_at=timezone.now()
        )
        call.status = Call.Status.MISSED
    signals = call.signals.exclude(sender=request.user).filter(
        pk__gt=int(after_id)
    ).order_by('pk')[:100]
    signals = list(signals)
    return JsonResponse({
        'call': call_payload(call),
        'offer': call.offer if call.recipient_id == request.user.pk else None,
        'answer': call.answer if call.caller_id == request.user.pk else None,
        'signals': [
            {'id': signal.pk, 'candidate': signal.candidate}
            for signal in signals
        ],
        'latest_signal_id': signals[-1].pk if signals else int(after_id),
    })


@login_required
@require_POST
def call_action(request, call_id):
    call = get_object_or_404(
        Call.objects.select_related('caller', 'recipient').filter(
            Q(caller=request.user) | Q(recipient=request.user)
        ),
        pk=call_id,
    )
    if call.caller_id in hidden_user_ids(request.user):
        raise Http404

    action = request.POST.get('action')
    now = timezone.now()
    if action == 'answer':
        if call.recipient_id != request.user.pk or call.status != Call.Status.RINGING:
            return JsonResponse({'error': 'This call can no longer be answered.'}, status=409)
        try:
            answer = json.loads(request.POST.get('answer', ''))
        except (TypeError, ValueError):
            return JsonResponse({'error': 'The call answer is invalid.'}, status=400)
        if (
            not isinstance(answer, dict)
            or answer.get('type') != 'answer'
            or not isinstance(answer.get('sdp'), str)
            or not answer['sdp']
            or len(answer['sdp']) > 50000
        ):
            return JsonResponse({'error': 'The call answer is invalid.'}, status=400)
        call.answer = answer
        call.status = Call.Status.ACTIVE
        call.answered_at = now
        call.save(update_fields=('answer', 'status', 'answered_at'))
        publish_call_state(call, answer=call.answer)
    elif action == 'decline':
        if call.recipient_id != request.user.pk or call.status != Call.Status.RINGING:
            return JsonResponse({'error': 'This call can no longer be declined.'}, status=409)
        call.status = Call.Status.DECLINED
        call.ended_at = now
        call.save(update_fields=('status', 'ended_at'))
        publish_call_state(call)
    elif action == 'end':
        if call.status not in (Call.Status.RINGING, Call.Status.ACTIVE):
            return JsonResponse({'error': 'This call has already ended.'}, status=409)
        call.status = Call.Status.ENDED
        call.ended_at = now
        call.save(update_fields=('status', 'ended_at'))
        publish_call_state(call)
    elif action == 'signal':
        if call.status not in (Call.Status.RINGING, Call.Status.ACTIVE):
            return JsonResponse({'error': 'This call is no longer available.'}, status=409)
        try:
            candidate = json.loads(request.POST.get('candidate', ''))
        except (TypeError, ValueError):
            return JsonResponse({'error': 'The connection signal is invalid.'}, status=400)
        if (
            not isinstance(candidate, dict)
            or not isinstance(candidate.get('candidate'), str)
            or len(candidate['candidate']) > 4096
        ):
            return JsonResponse({'error': 'The connection signal is invalid.'}, status=400)
        signal = CallSignal.objects.create(
            call=call, sender=request.user, candidate=candidate
        )
        recipient_id = call.recipient_id if call.caller_id == request.user.pk else call.caller_id
        send_call_event(recipient_id, {
            'event': 'call_signal',
            'call_id': call.pk,
            'signal_id': signal.pk,
            'candidate': candidate,
        })
    else:
        return JsonResponse({'error': 'Unknown call action.'}, status=400)

    return JsonResponse(call_payload(call))


@login_required
def conversation(request, username):
    other = get_object_or_404(User, username=username)
    if other == request.user or other.pk in hidden_user_ids(request.user):
        raise Http404
    thread = Message.objects.filter(
        Q(sender=request.user, recipient=other) | Q(sender=other, recipient=request.user)
    ).select_related('sender', 'recipient')
    if request.method == 'GET' and request.GET.get('after_id') is not None:
        after_id = request.GET.get('after_id', '')
        if not after_id.isdigit():
            return JsonResponse({'error': 'Invalid message cursor.'}, status=400)
        new_messages = thread.filter(pk__gt=int(after_id)).order_by('pk')[:100]
        thread.filter(
            pk__in=[message.pk for message in new_messages],
            recipient=request.user,
            read_at__isnull=True,
        ).update(read_at=timezone.now())
        return JsonResponse({
            'messages': [
                {
                    'id': message.pk,
                    'body': message.body,
                    'time': timezone.localtime(message.created_at).strftime('%I:%M %p').lstrip('0'),
                    'mine': message.sender_id == request.user.pk,
                    'html': render_to_string(
                        'social/_message.html', {'message': message}, request=request
                    ),
                }
                for message in new_messages
            ]
        })
    form = MessageForm(request.POST or None, request.FILES or None, user=request.user)
    if request.method == 'POST' and form.is_valid():
        message = form.save(commit=False)
        message.sender, message.recipient = request.user, other
        form.save_instance(message)
        Notification.objects.create(
            recipient=other, actor=request.user, kind=Notification.Kind.MESSAGE
        )
        if is_ajax(request):
            return JsonResponse({
                'id': message.pk,
                'body': message.body,
                'time': timezone.localtime(message.created_at).strftime('%I:%M %p').lstrip('0'),
                'mine': True,
                'html': render_to_string(
                    'social/_message.html', {'message': message}, request=request
                ),
            })
        return redirect('conversation', username=username)
    if request.method == 'POST' and is_ajax(request):
        errors = [
            str(error)
            for error_list in form.errors.values()
            for error in error_list
        ] or ['Could not send message.']
        return JsonResponse({'errors': errors}, status=400)
    thread.filter(sender=other, recipient=request.user, read_at__isnull=True).update(
        read_at=timezone.now()
    )
    sticker_form = StickerForm(user=request.user)
    stickers = request.user.stickers.all()
    last_message_id = thread.aggregate(latest=Max('pk'))['latest'] or 0
    return render(
        request, 'social/conversation.html',
        {'other': other, 'thread': thread, 'form': form, 'sticker_form': sticker_form,
         'stickers': stickers, 'last_message_id': last_message_id,
         'emojis': (
             '😀', '😂', '🥹', '😍', '🥰', '😘', '😎', '😭', '🤔', '🙌',
             '👏', '🔥', '✨', '❤️', '💚', '💯', '🎉', '👋', '👍', '🙏',
         )},
    )


@login_required
@require_POST
def create_sticker(request, username):
    other = get_object_or_404(User, username=username)
    if other == request.user or other.pk in hidden_user_ids(request.user):
        raise Http404
    form = StickerForm(request.POST, request.FILES, user=request.user)
    if not form.is_valid():
        return JsonResponse(
            {
                'errors': [
                    str(error)
                    for error_list in form.errors.values()
                    for error in error_list
                ] or ['Choose a valid sticker image.']
            },
            status=400,
        )
    sticker = form.save(commit=False)
    sticker.owner = request.user
    form.save_instance(sticker)
    return JsonResponse({
        'html': render_to_string(
            'social/_sticker_option.html', {'sticker': sticker}, request=request
        ),
    })


@login_required
def inbox(request):
    partners = User.objects.filter(
        Q(sent_messages__recipient=request.user) | Q(received_messages__sender=request.user)
    ).exclude(pk=request.user.pk).distinct().exclude(
        pk__in=hidden_user_ids(request.user)
    ).order_by('username')
    return render(request, 'social/inbox.html', {'partners': partners})


@login_required
@require_POST
def block_user(request, username):
    person = get_object_or_404(User, username=username)
    if person == request.user:
        raise Http404
    with transaction.atomic():
        Block.objects.get_or_create(blocker=request.user, blocked=person)
        Follow.objects.filter(
            Q(follower=request.user, followed=person) | Q(follower=person, followed=request.user)
        ).delete()
    messages.success(request, f'@{person.username} has been blocked.')
    return redirect('feed')


@login_required
def blocked_accounts(request):
    blocks = Block.objects.filter(blocker=request.user).select_related('blocked', 'blocked__profile')
    return render(request, 'social/blocked_accounts.html', {'blocks': blocks})


@login_required
@require_POST
def unblock_user(request, username):
    block = get_object_or_404(Block, blocker=request.user, blocked__username=username)
    block.delete()
    messages.success(request, f'@{username} has been unblocked.')
    return redirect('blocked_accounts')


@login_required
def report(request, target_type, target_id):
    if target_type not in {'post', 'user'}:
        raise Http404
    target_model = Post if target_type == 'post' else User
    target = get_object_or_404(target_model, pk=target_id)
    form = ReportForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        report_record = form.save(commit=False)
        report_record.reporter = request.user
        if target_type == 'post':
            report_record.post = target
        else:
            report_record.reported_user = target
        report_record.full_clean()
        report_record.save()
        messages.success(request, 'Thanks. Your report has been sent to the moderation team.')
        return redirect('feed')
    return render(request, 'social/report.html', {'form': form, 'target': target})
