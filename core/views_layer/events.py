from datetime import date
from functools import reduce

from django.db.models import ExpressionWrapper, F, IntegerField, Q
from rest_framework.permissions import IsAuthenticated
from rest_framework.viewsets import ModelViewSet
from rest_framework_simplejwt.authentication import JWTAuthentication

from core.models import Event, UserProfile, Subscription, WishList, Invitation
from core.serializers import ListUpdateEventSerializer, EventSerializer
from utils.common import api_error_response, api_success_response
from rest_framework.authentication import get_authorization_header
from utils.helper import send_email_sms_and_notification
from utils.s3 import AwsS3
from eon_backend.settings import SECRET_KEY, BUCKET
import jwt


class EventViewSet(ModelViewSet):
    authentication_classes = (JWTAuthentication,)
    permission_classes = (IsAuthenticated,)
    queryset = Event.objects.filter(is_active=True).select_related('type').annotate(event_type=F('type__type'))
    serializer_class = ListUpdateEventSerializer
    s3 = AwsS3()

    def list(self, request, *args, **kwargs):
        """
        Function to give list of Events based on different filter parameters
        :param request: contain the query type and it's value
        :return: Response contains complete list of events after the query
        """
        search_text = request.GET.get("search", None)
        event_type = request.GET.get("event_type", None)
        start_date = request.GET.get("start_date", None)
        end_date = request.GET.get("end_date", None)
        event_created_by = request.GET.get("event_created_by", False)
        is_wishlisted = request.GET.get('is_wishlisted', False)

        token = get_authorization_header(request).split()[1]
        payload = jwt.decode(token, SECRET_KEY)
        user_id = payload['user_id']

        try:
            user_logged_in = user_id
            user_role = UserProfile.objects.get(user_id=user_logged_in).role.role
        except Exception as err:
            return api_error_response(message="Not able to fetch the role of the logged in user", status=500)

        if is_wishlisted == 'True':
            try:
                event_ids = WishList.objects.filter(user=user_id).values_list('event__id', flat=True)
                self.queryset = self.queryset.filter(id__in=event_ids)
            except Exception as err:
                return api_error_response(message="Some internal error coming in fetching the wishlist", status=400)
        today = date.today()
        self.queryset.filter(date__lt=str(today)).update(is_active=False)
        self.queryset = self.queryset.filter(date__gte=str(today))

        if search_text:
            self.queryset = self.queryset.filter(Q(location__icontains=search_text) | Q(name__icontains=search_text))
        if event_created_by == 'True':
            self.queryset = self.queryset.filter(event_created_by=user_id)
        if event_type:
            self.queryset = self.queryset.filter(type=event_type)
        if start_date and end_date:
            self.queryset = self.queryset.filter(date__range=[start_date, end_date])
        if len(self.queryset) > 1:
            self.queryset = self.queryset.annotate(diff=ExpressionWrapper(
                F('sold_tickets') * 100000 / F('no_of_tickets'), output_field=IntegerField()))
            self.queryset = self.queryset.order_by('-diff')

        if user_role == 'subscriber':
            is_subscriber = True
        else:
            is_subscriber = False

        data = []

        for curr_event in self.queryset:
            response_obj = {"id": curr_event.id, "name": curr_event.name,
                            "date": curr_event.date, "time": curr_event.time,
                            "location": curr_event.location, "event_type": curr_event.type.id,
                            "description": curr_event.description,
                            "no_of_tickets": curr_event.no_of_tickets,
                            "sold_tickets": curr_event.sold_tickets,
                            "subscription_fee": curr_event.subscription_fee,
                            "images": self.s3.get_presigned_url(bucket_name=BUCKET,
                                                                object_name=curr_event.images),
                            "external_links": curr_event.external_links
                            }
            if is_subscriber:
                try:
                    Subscription.objects.get(user_id=user_logged_in, event_id=curr_event.id)
                    response_obj['is_subscribed'] = True
                except Subscription.DoesNotExist:
                    response_obj['is_subscribed'] = False

                try:
                    WishList.objects.get(user_id=user_logged_in, event_id=curr_event.id)
                    response_obj['is_wishlisted'] = True
                except WishList.DoesNotExist:
                    response_obj['is_wishlisted'] = False
            data.append(response_obj)

        return api_success_response(message="List of events", data=data)

    def create(self, request, *args, **kwargs):
        self.serializer_class = EventSerializer
        return super(EventViewSet, self).create(request, *args, **kwargs)

    def retrieve(self, request, *args, **kwargs):
        token = get_authorization_header(request).split()[1]
        payload = jwt.decode(token, SECRET_KEY)
        user_id = payload['user_id']

        try:
            user_logged_in = user_id
            user_role = UserProfile.objects.get(user_id=user_logged_in).role.role
        except Exception as err:
            return api_error_response(message="Not able to fetch the role of the logged in user", status=500)
        if user_role == 'subscriber':
            is_subscriber = True
        else:
            is_subscriber = False

        event_id = int(kwargs.get('pk'))
        try:
            curr_event = self.queryset.get(id=event_id)
        except Event.DoesNotExist:
            return api_error_response(message="Given event {} does not exist".format(event_id))

        if not is_subscriber:
            data = []
            if curr_event.event_created_by.id == user_logged_in:
                invitee_list = Invitation.objects.filter(event=curr_event.id, is_active=True)
            else:
                invitee_list = []
            invitee_data = []
            for invited in invitee_list:
                response_obj = {'invitation_id': invited.id, 'email': invited.email}
                if invited.user is not None:
                    try:
                        user_profile = UserProfile.objects.get(user=invited.user.id)
                        response_obj['user'] = {'user_id': invited.user.id, 'name': user_profile.name,
                                                'contact_number': user_profile.contact_number,
                                                'address': user_profile.address,
                                                'organization': user_profile.organization}
                    except UserProfile.DoesNotExist:
                        pass
                response_obj['discount_percentage'] = invited.discount_percentage
                invitee_data.append(response_obj)
            data.append({"id": curr_event.id, "name": curr_event.name,
                         "date": curr_event.date, "time": curr_event.time,
                         "location": curr_event.location, "event_type": curr_event.type.id,
                         "description": curr_event.description,
                         "no_of_tickets": curr_event.no_of_tickets,
                         "sold_tickets": curr_event.sold_tickets,
                         "subscription_fee": curr_event.subscription_fee,
                         "images": self.s3.get_presigned_url(bucket_name=BUCKET,
                                                             object_name=curr_event.images),
                         "external_links": curr_event.external_links,
                         "invitee_list": invitee_data
                         })

            return api_success_response(message="event details", data=data, status=200)
        else:
            data = {"id": curr_event.id, "name": curr_event.name,
                    "date": curr_event.date, "time": curr_event.time,
                    "location": curr_event.location, "event_type": curr_event.type.id,
                    "description": curr_event.description,
                    "subscription_fee": curr_event.subscription_fee,
                    "no_of_tickets": curr_event.no_of_tickets,
                    "images": self.s3.get_presigned_url(bucket_name=BUCKET,
                                                        object_name=curr_event.images),
                    "external_links": curr_event.external_links,
                    }
            try:
                # TODO: Return cumulative data of the subscription.
                # subscription_obj = Subscription.objects.filter(user_id=user_logged_in,
                #                                                event_id=curr_event.id,
                #                                                is_active=True)
                # subscription_data = []
                # amount_paid = reduce(
                #     lambda pre_amount, next_amount:
                #     {'amount': pre_amount['total_amount'] + next_amount['total_amount']}, subscription_obj
                # )
                # no_of_tickets_bought = reduce(
                #     lambda pre_count, next_count:
                #     {"count": pre_count["no_of_tickets"] + next_count["no_of_tickets"]}, subscription_obj
                # )
                # for subscription in subscription_obj:
                #     subscription_data.append({
                #         "is_subscribed": is_subscriber,
                #         "id": subscription.id,
                #         "no_of_tickets_bought": int(subscription.no_of_tickets),
                #         "amount_paid": subscription.payment.total_amount,
                #         "discount_given": subscription.payment.discount_amount,
                #         "discount_percentage": (subscription.payment.discount_amount /
                #                                 subscription.payment.amount) * 100
                #     })
                # data["subscription_details"] = subscription_data
                subscription_obj = Subscription.objects.get(user_id=user_logged_in,
                                                            event_id=curr_event.id,
                                                            is_active=True)
                data["subscription_details"] = {
                    "is_subscribed": is_subscriber,
                    "id": subscription_obj.id,
                    "no_of_tickets_bought": int(subscription_obj.no_of_tickets),
                    "amount_paid": subscription_obj.payment.total_amount,
                    "discount_given": subscription_obj.payment.discount_amount,
                    "discount_percentage": (subscription_obj.payment.discount_amount /
                                            subscription_obj.payment.amount) * 100
                }
            except Subscription.DoesNotExist:
                try:
                    discount_allotted = Invitation.objects.get(user=user_id,
                                                               event=curr_event.id,
                                                               is_active=True).discount_percentage
                except Invitation.DoesNotExist:
                    discount_allotted = 0
                data['discount_percentage'] = discount_allotted
                data["subscription_details"] = dict()
            return api_success_response(message="Event details", data=data, status=200)

    def destroy(self, request, *args, **kwargs):
        event_id = int(kwargs.get('pk'))
        data = request.data
        message = data.get("message", "")
        try:
            self.queryset.get(id=event_id)
        except Event.DoesNotExist:
            return api_error_response(message="Given event id {} does not exist".format(event_id))
        try:
            user_obj = Subscription.objects.filter(event=event_id).select_related('user').annotate(
                email=F('user__email'), users_id=F('user__id')).values("email", "users_id")
            email_ids = [_["email"] for _ in user_obj]
            user_ids = [_["users_id"] for _ in user_obj]
        except Subscription.DoesNotExist:
            email_ids = []
            user_ids = []
        self.queryset.filter(id=event_id).update(is_active=False)
        send_email_sms_and_notification(action_name="event_deleted",
                                        email_ids=email_ids,
                                        message=message,
                                        user_ids=user_ids,
                                        event_id=event_id)
        return api_success_response(message="Event successfully deleted", status=200)
