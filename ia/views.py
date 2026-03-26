from django.contrib.auth.decorators import login_required
from django.shortcuts import render


@login_required
def creator(request):
    return render(request, 'ia/creator.html')
