import json
from datetime import date

from django.db import transaction
from django.db.models import ExpressionWrapper, IntegerField, Value
from django.db.models import F
from rest_framework import mixins, generics
from rest_framework.viewsets import ModelViewSet
from rest_framework_simplejwt.authentication import JWTAuthentication

from core.models import Event, Subscription, EventStatus
from core.serializers import EventSerializer, SubscriptionSerializer, ListUpdateEventSerializer, \
    SubscriptionListSerializer
from utils.common import api_success_response, api_error_response


# Create your views here.


class EventViewSet(ModelViewSet):
    authentication_classes = (JWTAuthentication,)
    queryset = Event.objects.all().select_related('type').annotate(event_type=F('type__type'))
    serializer_class = ListUpdateEventSerializer

    def list(self, request, *args, **kwargs):
        location = request.GET.get("location", None)
        event_type = request.GET.get("event_type", None)
        start_date = request.GET.get("start_date", None)
        end_date = request.GET.get("end_date", None)
        today = date.today()
        status = EventStatus.objects.get(type__iexact="Cancelled")
        self.queryset.filter(date__lt=str(today)).update(status=status.id)
        self.queryset = self.queryset.filter(date__gte=str(today))
        if location:
            self.queryset = self.queryset.filter(location__iexact=location, status__type='ACTIVE')
        if event_type:
            self.queryset = self.queryset.filter(type=event_type, status__type='ACTIVE')
        if start_date and end_date:
            self.queryset = self.queryset.filter(date__range=[start_date, end_date], status__type='ACTIVE')
        if len(self.queryset) > 1:
            self.queryset = self.queryset.annotate(diff=ExpressionWrapper(
                F('sold_tickets')*100000/F('no_of_tickets'), output_field=IntegerField()))
            self.queryset = self.queryset.order_by('-diff')
        return super(EventViewSet, self).list(request, *args, **kwargs)

    def create(self, request, *args, **kwargs):
        self.serializer_class = EventSerializer
        return super(EventViewSet, self).create(request, *args, **kwargs)


class SubscriptionViewSet(mixins.ListModelMixin, mixins.CreateModelMixin, generics.GenericAPIView):
    authentication_classes = (JWTAuthentication,)
    queryset = Subscription.objects.all()

    def get(self, request, *args, **kwargs):
        event_id = request.GET.get("event_id", None)
        if event_id:
            self.queryset = self.queryset.filter(event=event_id)

        self.queryset = self.queryset.select_related('user').annotate(email=F('user__email'),
                                                                      name=F('user__userdetails__name'),
                                                                      contact_number=F(
                                                                          'user__userdetails__contact_number'))

        queryset = self.queryset.filter(payment__isnull=True)
        self.queryset = self.queryset.filter(payment__isnull=False)
        self.queryset = self.queryset.select_related('payment').annotate(paid_amount=F('payment__total_amount'))
        queryset = queryset.select_related('payment').annotate(paid_amount=Value(0, IntegerField()))
        queryset = self.queryset.union(queryset)
        serializer = SubscriptionListSerializer(queryset, many=True)
        data = dict(total=len(queryset), subscribtion_list=serializer.data)
        return api_success_response(data=data, status=200)

    @transaction.atomic()
    def post(self, request):
        """
            Function to set subscription of a user to a particular event
            :param request: token, event_id, no_of_tickets, payment
            :return: json response subscribed successful or error message
        """
        data = json.loads(request.body)
        event_id = data.get('event_id', None)
        no_of_tickets = data.get('no_of_tickets', None)
        payment_id = data.get('payment_id', None)
        user_id = data.get('user_id', None)
        # getting user_id from token
        if not event_id or not no_of_tickets or not user_id:
            return api_error_response(message="Required Fields are not present")

        data = dict(user=user_id, event=event_id, no_of_tickets=no_of_tickets, payment=payment_id)
        try:
            self.event = Event.objects.get(id=event_id)
        except:
            return api_error_response("Invalid event_id")

        if self.event.subscription_fee > 0 and not payment_id:
            return api_error_response(message="Required Fields are not present")

        if self.event.no_of_tickets - self.event.sold_tickets >= no_of_tickets:
            serializer = SubscriptionSerializer(data=data)
            serializer.is_valid(raise_exception=True)
            serializer.save()

            if serializer.instance.payment:
                queryset = Subscription.objects.filter(id=serializer.instance.id)
                queryset = queryset.select_related('payment')
                queryset = queryset.select_related('event')
                queryset = queryset.annotate(pay=F('payment__id'), amount=F('payment__total_amount'),
                                             events=F('event'), event_name=F('event__name'),
                                             event_date=F('event__date'), event_time=F('event__time'),
                                             event_location=F('event__location'))
                data = dict(no_of_tickets=queryset[0].no_of_tickets, payment_id=queryset[0].pay,
                            amount=queryset[0].amount, event_name=queryset[0].event_name,
                            event_date=queryset[0].event_date, event_time=queryset[0].event_time,
                            event_location=queryset[0].event_location)

            return api_success_response(message="Subscribed Successfully", data=data, status=201)
        else:
            return api_error_response(message="Number of tickets are invalid", status=400)
