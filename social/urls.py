from django.contrib.auth import views as auth_views
from django.urls import path

from . import views
from .forms import EmailOrUsernameAuthenticationForm

urlpatterns = [
    path('', views.feed, name='feed'),
    path('media/private/<path:path>', views.private_media, name='private_media'),
    path('uploads/token/', views.blob_upload_token, name='blob_upload_token'),
    path('uploads/complete/', views.blob_upload_complete, name='blob_upload_complete'),
    path('feed/updates/', views.feed_updates, name='feed_updates'),
    path('explore/', views.explore, name='explore'),
    path('signup/', views.signup, name='signup'),
    path(
        'login/',
        auth_views.LoginView.as_view(
            template_name='social/login.html',
            authentication_form=EmailOrUsernameAuthenticationForm,
        ),
        name='login',
    ),
    path('logout/', auth_views.LogoutView.as_view(), name='logout'),
    path('create/', views.create_post, name='create_post'),
    path('stories/create/', views.create_story, name='create_story'),
    path('stories/<int:story_id>/', views.story_detail, name='story_detail'),
    path('reels/', views.reels, name='reels'),
    path('activity/', views.activity, name='activity'),
    path('activity/updates/', views.activity_updates, name='activity_updates'),
    path('calls/incoming/', views.incoming_calls, name='incoming_calls'),
    path('calls/start/<str:username>/', views.start_call, name='start_call'),
    path('calls/<int:call_id>/updates/', views.call_updates, name='call_updates'),
    path('calls/<int:call_id>/action/', views.call_action, name='call_action'),
    path('inbox/', views.inbox, name='inbox'),
    path('inbox/<str:username>/', views.conversation, name='conversation'),
    path('inbox/<str:username>/stickers/', views.create_sticker, name='create_sticker'),
    path('profile/edit/', views.edit_profile, name='edit_profile'),
    path('settings/', views.account_settings, name='account_settings'),
    path(
        'settings/password/',
        views.SocialPasswordChangeView.as_view(),
        name='password_change',
    ),
    path('profile/<str:username>/', views.profile, name='profile'),
    path('follow/<str:username>/', views.toggle_follow, name='toggle_follow'),
    path('posts/<int:post_id>/like/', views.toggle_like, name='toggle_like'),
    path('posts/<int:post_id>/comment/', views.add_comment, name='add_comment'),
    path('comments/<int:comment_id>/like/', views.toggle_comment_like, name='toggle_comment_like'),
    path('block/<str:username>/', views.block_user, name='block_user'),
    path('blocked/', views.blocked_accounts, name='blocked_accounts'),
    path('unblock/<str:username>/', views.unblock_user, name='unblock_user'),
    path('report/<str:target_type>/<int:target_id>/', views.report, name='report'),
]
