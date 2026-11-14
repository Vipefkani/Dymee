import json
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.conf import settings
from django.test import RequestFactory, TestCase, override_settings
from django.urls import resolve, reverse
from django.utils import timezone
from PIL import Image
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from channels.testing import WebsocketCommunicator

from config.urls import media_urlpatterns

from .consumers import CallConsumer
from .blob_uploads import create_upload_ticket, get_valid_ticket, sign_ticket
from .forms import CommentForm, MessageForm, PostForm
from .models import (
    Block, Call, Comment, Follow, Like, Message, Notification, Post, Sticker,
)


class SocialAppTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username='maker', email='maker@example.test', password='Strong-pass-123!'
        )
        self.viewer = User.objects.create_user(
            username='viewer', email='viewer@example.test', password='Strong-pass-123!'
        )
        self.post = Post.objects.create(author=self.owner, media='posts/test.jpg', caption='hello')

    @override_settings(BLOB_UPLOAD_ENABLED=True, BLOB_UPLOAD_SIGNING_SECRET='test-upload-secret')
    def test_blob_upload_ticket_is_bound_to_user_and_consumed_when_saved(self):
        ticket = create_upload_ticket(self.owner, 'post', 'moment.jpg', 128)
        token = sign_ticket(ticket)
        ticket.completed_at = timezone.now()
        ticket.save(update_fields=('completed_at',))
        form = PostForm(
            {
                'caption': 'A remotely uploaded post',
                'location': '',
                'media_filter': 'original',
                'blob_upload_token': token,
                'blob_upload_path': ticket.pathname,
            },
            user=self.owner,
        )

        self.assertTrue(form.is_valid(), form.errors)
        post = form.save(commit=False)
        post.author = self.owner
        post.media_type = form.media_type
        form.save_instance(post)

        post.refresh_from_db()
        ticket.refresh_from_db()
        self.assertEqual(post.media.name, ticket.pathname)
        self.assertIsNotNone(ticket.consumed_at)
        with self.assertRaises(ValidationError):
            get_valid_ticket(self.owner, 'post', token, ticket.pathname)

    @override_settings(
        BLOB_UPLOAD_ENABLED=True,
        BLOB_UPLOAD_SIGNING_SECRET='test-upload-secret',
        BLOB_READ_WRITE_TOKEN='test-token',
        BLOB_STORE_ID='test-store',
    )
    @patch('social.views.verify_uploaded_blob')
    def test_blob_upload_completion_requires_a_valid_signed_path(self, verify_blob):
        ticket = create_upload_ticket(self.owner, 'post', 'moment.jpg', 128)
        token = sign_ticket(ticket)
        response = self.client.post(
            reverse('blob_upload_complete'),
            json.dumps({'token': token, 'pathname': 'uploads/post/other/moment.jpg'}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)
        ticket.refresh_from_db()
        self.assertIsNone(ticket.completed_at)

        response = self.client.post(
            reverse('blob_upload_complete'),
            json.dumps({'token': token, 'pathname': ticket.pathname}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        ticket.refresh_from_db()
        self.assertIsNotNone(ticket.completed_at)
        verify_blob.assert_called_once_with(ticket)

    @override_settings(BLOB_UPLOAD_ENABLED=True)
    def test_private_chat_media_is_only_available_to_conversation_participants(self):
        message = Message.objects.create(
            sender=self.owner,
            recipient=self.viewer,
            attachment='uploads/chat/ticket/private.pdf',
        )
        self.client.force_login(self.owner)
        upstream = Mock()
        upstream.status = 200
        upstream.headers = {'Content-Length': '7'}
        upstream.read = Mock(side_effect=[b'private', b''])
        upstream.close = Mock()
        with patch('social.views.default_storage') as storage:
            storage.open_response.return_value = upstream
            response = self.client.get(reverse('private_media', args=(message.attachment.name,)))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(b''.join(response.streaming_content), b'private')

        stranger = User.objects.create_user(username='stranger', password='Strong-pass-456!')
        self.client.force_login(stranger)
        response = self.client.get(reverse('private_media', args=(message.attachment.name,)))
        self.assertEqual(response.status_code, 404)

    def test_user_creation_creates_profile(self):
        self.assertTrue(self.owner.profile)

    def test_profile_uses_root_relative_static_and_media_urls(self):
        self.client.force_login(self.viewer)
        response = self.client.get(reverse('profile', args=(self.owner.username,)))
        self.assertContains(response, 'href="/static/social/app.css"')
        self.assertContains(response, 'aria-label="Main navigation"')
        self.assertContains(response, 'Blocked accounts')
        self.assertContains(response, 'Log out')
        Follow.objects.create(follower=self.viewer, followed=self.owner)
        response = self.client.get(reverse('feed'))
        self.assertContains(response, 'src="/media/posts/test.jpg"')

    def test_local_media_route_serves_files_when_explicitly_enabled(self):
        with TemporaryDirectory() as media_root:
            (Path(media_root) / 'test.mp4').write_bytes(b'test video')
            with override_settings(
                DEBUG=False, SERVE_MEDIA_FILES=True, MEDIA_ROOT=media_root
            ):
                request = RequestFactory().get('/media/test.mp4')
                match = resolve('/media/test.mp4', urlconf=tuple(media_urlpatterns()))
                response = match.func(request, *match.args, **match.kwargs)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response['Content-Type'], 'video/mp4')
                response.close()

    def test_email_login(self):
        response = self.client.post(
            reverse('login'),
            {'username': self.owner.email, 'password': 'Strong-pass-123!'},
        )
        self.assertRedirects(response, reverse('feed'))
        self.assertTrue(response.wsgi_request.user.is_authenticated)

    def test_branding_uses_dymee(self):
        response = self.client.get(reverse('login'))
        self.assertContains(response, '<title>Sign in · Dymee</title>')
        self.assertContains(response, '> dymee</a>')

    def test_homepage_redirects_to_the_dymee_login_page(self):
        response = self.client.get('/', HTTP_HOST='127.0.0.1')
        self.assertRedirects(response, '/login/?next=/')
        login_response = self.client.get(response['Location'], HTTP_HOST='127.0.0.1')
        self.assertEqual(login_response.status_code, 200)
        self.assertContains(login_response, '<title>Sign in · Dymee</title>')

    def test_debug_is_off_by_default_and_unknown_urls_show_branded_404(self):
        self.assertFalse(settings.DEBUG)
        response = self.client.get('/this-page-does-not-exist/')
        self.assertEqual(response.status_code, 404)
        self.assertContains(response, "This page isn't here.", status_code=404)
        self.assertNotContains(
            response,
            'You’re seeing this error because you have DEBUG',
            status_code=404,
        )

    def test_account_settings_and_password_change_require_login(self):
        response = self.client.get(reverse('account_settings'))
        self.assertEqual(response.status_code, 302)
        self.client.force_login(self.viewer)
        response = self.client.post(
            reverse('account_settings'),
            {'username': 'new-viewer', 'email': 'new-viewer@example.test'},
        )
        self.assertRedirects(response, reverse('account_settings'))
        self.viewer.refresh_from_db()
        self.assertEqual(self.viewer.username, 'new-viewer')
        self.assertEqual(self.viewer.email, 'new-viewer@example.test')
        response = self.client.post(
            reverse('password_change'),
            {
                'old_password': 'Strong-pass-123!',
                'new_password1': 'Even-stronger-pass-456!',
                'new_password2': 'Even-stronger-pass-456!',
            },
        )
        self.assertRedirects(response, reverse('account_settings'))
        self.viewer.refresh_from_db()
        self.assertTrue(self.viewer.check_password('Even-stronger-pass-456!'))

    def test_feed_shows_followed_posts_but_hides_blocked_users(self):
        Follow.objects.create(follower=self.viewer, followed=self.owner)
        self.client.force_login(self.viewer)
        response = self.client.get(reverse('feed'))
        self.assertContains(response, 'hello')

        Block.objects.create(blocker=self.viewer, blocked=self.owner)
        response = self.client.get(reverse('feed'))
        self.assertNotContains(response, 'hello')

    def test_like_and_comment_actions_are_authenticated_and_persisted(self):
        self.client.force_login(self.viewer)
        self.client.post(reverse('toggle_like', args=(self.post.pk,)))
        self.assertTrue(Like.objects.filter(user=self.viewer, post=self.post).exists())
        self.client.post(
            reverse('add_comment', args=(self.post.pk,)), {'body': 'nice photo'}
        )
        self.assertTrue(Comment.objects.filter(post=self.post, author=self.viewer).exists())

    def test_ajax_like_and_comment_return_updates_without_redirects(self):
        self.client.force_login(self.viewer)
        like_response = self.client.post(
            reverse('toggle_like', args=(self.post.pk,)),
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
            HTTP_ACCEPT='application/json',
        )
        self.assertEqual(like_response.status_code, 200)
        self.assertEqual(like_response.json()['like_count'], 1)

        comment_response = self.client.post(
            reverse('add_comment', args=(self.post.pk,)),
            {'body': 'live update'},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
            HTTP_ACCEPT='application/json',
        )
        self.assertEqual(comment_response.status_code, 200)
        self.assertIn('live update', comment_response.json()['html'])
        self.assertEqual(comment_response.json()['comment_count'], 1)

    def test_feed_updates_only_returns_new_followed_content(self):
        Follow.objects.create(follower=self.viewer, followed=self.owner)
        new_post = Post.objects.create(author=self.owner, media='posts/new.jpg', caption='newly shared')
        self.client.force_login(self.viewer)
        response = self.client.get(
            reverse('feed_updates'),
            {'after_id': self.post.pk},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn('newly shared', response.json()['html'])
        self.assertEqual(response.json()['latest_id'], new_post.pk)

    def test_activity_updates_returns_only_new_notifications(self):
        self.client.force_login(self.viewer)
        initial = self.client.get(reverse('activity_updates'))
        self.assertEqual(initial.status_code, 200)
        cursor = initial.json()['latest_id']
        notification = Notification.objects.create(
            recipient=self.viewer, actor=self.owner, kind=Notification.Kind.FOLLOW
        )

        response = self.client.get(
            reverse('activity_updates'),
            {'after_id': cursor},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['unread_count'], 1)
        self.assertEqual(response.json()['notifications'], [{
            'id': notification.pk,
            'actor': self.owner.username,
            'kind': Notification.Kind.FOLLOW,
            'message': 'started following you',
        }])

    def test_activity_updates_rejects_invalid_cursors_and_hides_blocked_actors(self):
        self.client.force_login(self.viewer)
        invalid = self.client.get(reverse('activity_updates'), {'after_id': 'nope'})
        self.assertEqual(invalid.status_code, 400)
        notification = Notification.objects.create(
            recipient=self.viewer, actor=self.owner, kind=Notification.Kind.LIKE
        )
        Block.objects.create(blocker=self.viewer, blocked=self.owner)
        response = self.client.get(
            reverse('activity_updates'), {'after_id': notification.pk - 1}
        )
        self.assertEqual(response.json()['notifications'], [])
        self.assertEqual(response.json()['unread_count'], 0)

    def test_ajax_follow_and_message_updates(self):
        self.client.force_login(self.viewer)
        follow_response = self.client.post(
            reverse('toggle_follow', args=(self.owner.username,)),
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertTrue(follow_response.json()['following'])
        self.assertEqual(follow_response.json()['follower_count'], 1)

        message_response = self.client.post(
            reverse('conversation', args=(self.owner.username,)),
            {'body': 'hello without reload'},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(message_response.status_code, 200)
        message_id = message_response.json()['id']
        self.assertTrue(Message.objects.filter(pk=message_id).exists())
        self.assertTrue(Notification.objects.filter(
            recipient=self.owner, actor=self.viewer, kind=Notification.Kind.MESSAGE
        ).exists())
        self.client.force_login(self.owner)
        poll_response = self.client.get(
            reverse('conversation', args=(self.viewer.username,)),
            {'after_id': 0},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(poll_response.json()['messages'][0]['body'], 'hello without reload')

    def test_comment_moderation_uses_configured_blocked_terms(self):
        with override_settings(SOCIAL_BLOCKED_TERMS=('bad phrase',)):
            form = CommentForm({'body': 'That is a bad phrase.'})
            self.assertFalse(form.is_valid())

    def test_blocked_people_cannot_message_each_other(self):
        Block.objects.create(blocker=self.owner, blocked=self.viewer)
        self.client.force_login(self.viewer)
        response = self.client.get(reverse('conversation', args=(self.owner.username,)))
        self.assertEqual(response.status_code, 404)

    def test_chat_renders_audio_and_video_call_controls(self):
        self.client.force_login(self.viewer)
        response = self.client.get(reverse('conversation', args=(self.owner.username,)))
        self.assertContains(response, 'Start audio call')
        self.assertContains(response, 'Start video call')
        self.assertContains(response, 'data-call-overlay')
        self.assertContains(response, 'data-call-answer')
        self.assertContains(response, 'data-emoji')
        self.assertContains(response, 'Attach a file')
        self.assertContains(response, 'My stickers')

    def test_audio_video_call_rings_recipient_and_exchanges_signals(self):
        offer = {'type': 'offer', 'sdp': 'test offer sdp'}
        self.client.force_login(self.viewer)
        start = self.client.post(
            reverse('start_call', args=(self.owner.username,)),
            {'kind': Call.Kind.VIDEO, 'offer': json.dumps(offer)},
        )
        self.assertEqual(start.status_code, 201)
        call_id = start.json()['id']

        self.client.force_login(self.owner)
        incoming = self.client.get(reverse('incoming_calls'))
        self.assertEqual(incoming.json()['calls'][0]['id'], call_id)
        self.assertEqual(incoming.json()['calls'][0]['offer'], offer)

        answer = {'type': 'answer', 'sdp': 'test answer sdp'}
        accepted = self.client.post(
            reverse('call_action', args=(call_id,)),
            {'action': 'answer', 'answer': json.dumps(answer)},
        )
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(accepted.json()['status'], Call.Status.ACTIVE)

        candidate = {'candidate': 'candidate:1 1 UDP 1 127.0.0.1 9 typ host'}
        signaled = self.client.post(
            reverse('call_action', args=(call_id,)),
            {'action': 'signal', 'candidate': json.dumps(candidate)},
        )
        self.assertEqual(signaled.status_code, 200)

        self.client.force_login(self.viewer)
        updates = self.client.get(reverse('call_updates', args=(call_id,)))
        self.assertEqual(updates.json()['answer'], answer)
        self.assertEqual(updates.json()['signals'][0]['candidate'], candidate)

        ended = self.client.post(
            reverse('call_action', args=(call_id,)), {'action': 'end'}
        )
        self.assertEqual(ended.json()['status'], Call.Status.ENDED)

    def test_call_signaling_rejects_bad_offers_and_other_users(self):
        self.client.force_login(self.viewer)
        invalid = self.client.post(
            reverse('start_call', args=(self.owner.username,)),
            {'kind': Call.Kind.AUDIO, 'offer': '{}'},
        )
        self.assertEqual(invalid.status_code, 400)
        started = self.client.post(
            reverse('start_call', args=(self.owner.username,)),
            {
                'kind': Call.Kind.AUDIO,
                'offer': json.dumps({'type': 'offer', 'sdp': 'offer'}),
            },
        )
        call_id = started.json()['id']
        self.client.force_login(User.objects.create_user(
            username='stranger', password='Strong-pass-456!'
        ))
        self.assertEqual(self.client.get(reverse('call_updates', args=(call_id,))).status_code, 404)
        self.assertEqual(
            self.client.post(
                reverse('call_action', args=(call_id,)), {'action': 'answer'}
            ).status_code,
            404,
        )

    def test_call_rejects_blocked_and_busy_participants(self):
        self.client.force_login(self.viewer)
        Block.objects.create(blocker=self.owner, blocked=self.viewer)
        blocked = self.client.post(
            reverse('start_call', args=(self.owner.username,)),
            {
                'kind': Call.Kind.AUDIO,
                'offer': json.dumps({'type': 'offer', 'sdp': 'offer'}),
            },
        )
        self.assertEqual(blocked.status_code, 404)

        third_user = User.objects.create_user(
            username='third-user', password='Strong-pass-789!'
        )
        Call.objects.create(
            caller=self.owner,
            recipient=third_user,
            kind=Call.Kind.AUDIO,
            status=Call.Status.ACTIVE,
            offer={'type': 'offer', 'sdp': 'offer'},
        )
        self.client.force_login(third_user)
        busy = self.client.post(
            reverse('start_call', args=(self.viewer.username,)),
            {
                'kind': Call.Kind.AUDIO,
                'offer': json.dumps({'type': 'offer', 'sdp': 'offer'}),
            },
        )
        self.assertEqual(busy.status_code, 409)

    def test_authenticated_call_websocket_receives_live_events(self):
        async def receive_call_event():
            communicator = WebsocketCommunicator(CallConsumer.as_asgi(), '/ws/calls/')
            communicator.scope['user'] = self.viewer
            connected, _ = await communicator.connect()
            await get_channel_layer().group_send(
                f'calls_{self.viewer.pk}',
                {
                    'type': 'call.event',
                    'payload': {'event': 'call_offer', 'call': {'id': 42}},
                },
            )
            event = await communicator.receive_json_from(timeout=1)
            await communicator.disconnect()
            return connected, event

        connected, event = async_to_sync(receive_call_event)()
        self.assertTrue(connected)
        self.assertEqual(event, {'event': 'call_offer', 'call': {'id': 42}})

    async def test_starting_a_call_sends_an_immediate_offer_to_the_other_user(self):
        communicator = WebsocketCommunicator(CallConsumer.as_asgi(), '/ws/calls/')
        communicator.scope['user'] = self.owner
        connected, _ = await communicator.connect()
        self.assertTrue(connected)
        await self.async_client.aforce_login(self.viewer)

        response = await self.async_client.post(
            reverse('start_call', args=(self.owner.username,)),
            {
                'kind': Call.Kind.AUDIO,
                'offer': json.dumps({'type': 'offer', 'sdp': 'offer sdp'}),
            },
        )
        self.assertEqual(response.status_code, 201)
        event = await communicator.receive_json_from(timeout=1)
        self.assertEqual(event['event'], 'call_offer')
        self.assertEqual(event['call']['id'], response.json()['id'])
        self.assertEqual(event['call']['caller'], self.viewer.username)
        self.assertEqual(event['offer']['sdp'], 'offer sdp')
        await communicator.disconnect()

    def test_chat_file_upload_is_available_without_reloading(self):
        self.client.force_login(self.viewer)
        with TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            attachment = SimpleUploadedFile(
                'notes.pdf', b'%PDF-1.7 safe sample', content_type='application/pdf'
            )
            response = self.client.post(
                reverse('conversation', args=(self.owner.username,)),
                {'body': 'See attached', 'attachment': attachment},
                HTTP_X_REQUESTED_WITH='XMLHttpRequest',
            )
            self.assertEqual(response.status_code, 200)
            self.assertIn('notes.pdf', response.json()['html'])
            self.assertTrue(Message.objects.filter(sender=self.viewer, attachment__endswith='.pdf').exists())

    def test_chat_rejects_executable_uploads(self):
        form = MessageForm(
            {'body': ''},
            {'attachment': SimpleUploadedFile('payload.exe', b'MZnot-an-image')},
            user=self.viewer,
        )
        self.assertFalse(form.is_valid())
        self.assertIn('attachment', form.errors)

    def test_users_can_upload_and_send_their_own_stickers(self):
        self.client.force_login(self.viewer)
        with TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            image_data = BytesIO()
            Image.new('RGB', (2, 2), color='green').save(image_data, format='PNG')
            response = self.client.post(
                reverse('create_sticker', args=(self.owner.username,)),
                {
                    'label': 'Happy face',
                    'image': SimpleUploadedFile(
                        'happy.png', image_data.getvalue(),
                        content_type='image/png',
                    ),
                },
                HTTP_X_REQUESTED_WITH='XMLHttpRequest',
            )
            self.assertEqual(response.status_code, 200)
            sticker = Sticker.objects.get(owner=self.viewer)
            self.assertIn('Happy face', response.json()['html'])
            response = self.client.post(
                reverse('conversation', args=(self.owner.username,)),
                {'sticker': sticker.pk},
                HTTP_X_REQUESTED_WITH='XMLHttpRequest',
            )
            self.assertEqual(response.status_code, 200)
            self.assertIn('happy.png', response.json()['html'])
            self.assertTrue(Message.objects.filter(sender=self.viewer, sticker=sticker).exists())

    def test_users_cannot_send_another_accounts_sticker(self):
        sticker = Sticker.objects.create(
            owner=self.owner, image='stickers/owner.png', label='Private sticker'
        )
        form = MessageForm(
            {'sticker': sticker.pk},
            user=self.viewer,
        )
        self.assertFalse(form.is_valid())
        self.assertIn('sticker', form.errors)

    def test_blocked_account_can_be_unblocked(self):
        Block.objects.create(blocker=self.viewer, blocked=self.owner)
        self.client.force_login(self.viewer)
        response = self.client.post(reverse('unblock_user', args=(self.owner.username,)))
        self.assertRedirects(response, reverse('blocked_accounts'))
        self.assertFalse(Block.objects.filter(blocker=self.viewer, blocked=self.owner).exists())

    def test_like_action_rejects_external_return_url(self):
        self.client.force_login(self.viewer)
        response = self.client.post(
            reverse('toggle_like', args=(self.post.pk,)),
            HTTP_REFERER='https://attacker.example/',
        )
        self.assertEqual(response['Location'], reverse('feed'))
