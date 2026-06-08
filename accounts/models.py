from django.contrib.auth.models import AbstractUser, UserManager
from django.db import models


class EmailUserManager(UserManager):
    def create_user(self, email=None, password=None, **extra_fields):
        email = self.normalize_email(email)
        if not email:
            raise ValueError("The email address must be set.")
        user = self.model(email=email, username=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email=None, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("is_active", True)

        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")

        return self.create_user(email=email, password=password, **extra_fields)


class User(AbstractUser):
    username = models.CharField(max_length=150, unique=True, blank=True)
    email = models.EmailField(unique=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    objects = EmailUserManager()

    def save(self, *args, **kwargs):
        self.email = type(self).objects.normalize_email(self.email)
        self.username = self.email
        super().save(*args, **kwargs)

    def __str__(self):
        return self.email
