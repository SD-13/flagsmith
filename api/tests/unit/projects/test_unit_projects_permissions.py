from unittest import TestCase, mock

import pytest
from django.conf import settings
from rest_framework.exceptions import APIException, PermissionDenied

from organisations.models import Organisation, OrganisationRole
from projects.models import (
    Project,
    ProjectPermissionModel,
    UserPermissionGroupProjectPermission,
    UserProjectPermission,
)
from projects.permissions import (
    VIEW_PROJECT,
    IsProjectAdmin,
    ProjectPermissions,
)
from users.models import FFAdminUser, UserPermissionGroup

mock_request = mock.MagicMock()
mock_view = mock.MagicMock()


@pytest.mark.django_db
class UserPermissionGroupProjectPermissionsTestCase(TestCase):
    def setUp(self) -> None:
        self.organisation = Organisation.objects.create(name="Test organisation")
        self.project = Project.objects.create(
            name="Test project", organisation=self.organisation
        )

        self.user = FFAdminUser.objects.create(email="user@test.com")
        self.user_permission_group = UserPermissionGroup.objects.create(
            name="Users", organisation=self.organisation
        )
        self.user_permission_group.users.add(self.user)
        self.user.add_organisation(self.organisation, OrganisationRole.USER)

        self.project_permissions = ProjectPermissions()

        self.read_permission = ProjectPermissionModel.objects.get(key=VIEW_PROJECT)

    def test_list_project(self):
        """All users should be able to create project"""
        # Given
        mock_view.action = "list"
        mock_view.detail = False

        # When
        mock_request.user = self.user
        self.project_permissions.has_permission(mock_request, mock_view)

        # Then - no exception raised

    def test_create_project(self):
        """All users should be able to create project"""
        # Given
        mock_view.action = "create"
        mock_view.detail = False
        mock_request.data = {"name": "Test", "organisation": self.organisation.id}

        # When
        mock_request.user = self.user
        self.project_permissions.has_permission(mock_request, mock_view)

        # Then - no exception raised

    def test_admin_group_can_update_project(self):
        # Given
        UserPermissionGroupProjectPermission.objects.create(
            group=self.user_permission_group, admin=True, project=self.project
        )
        mock_view.action = "update"
        mock_view.detail = True
        mock_request.user = self.user

        # When
        result = self.project_permissions.has_object_permission(
            mock_request, mock_view, self.project
        )

        # Then
        assert result

    def test_admin_user_can_update_project(self):
        # Given
        UserProjectPermission.objects.create(
            user=self.user, admin=True, project=self.project
        )
        mock_view.action = "update"
        mock_view.detail = True
        mock_request.user = self.user

        # When
        result = self.project_permissions.has_object_permission(
            mock_request, mock_view, self.project
        )

        # Then
        assert result

    def test_admin_user_and_admin_group_can_update_project(self):
        # Given
        UserProjectPermission.objects.create(
            user=self.user, admin=True, project=self.project
        )
        UserPermissionGroupProjectPermission.objects.create(
            group=self.user_permission_group, admin=True, project=self.project
        )
        mock_view.action = "update"
        mock_view.detail = True
        mock_request.user = self.user

        # When
        result = self.project_permissions.has_object_permission(
            mock_request, mock_view, self.project
        )

        # Then
        assert result

    def test_regular_user_cannot_update_project(self):
        # Given
        user_project_permission = UserProjectPermission.objects.create(
            user=self.user, project=self.project
        )
        user_project_permission.permissions.add(self.read_permission)

        mock_view.action = "update"
        mock_view.detail = True
        mock_request.user = self.user

        # When
        result = self.project_permissions.has_object_permission(
            mock_request, mock_view, self.project
        )

        # Then
        assert not result

    def test_admin_can_delete_project(self):
        # Given
        UserPermissionGroupProjectPermission.objects.create(
            group=self.user_permission_group, admin=True, project=self.project
        )
        mock_view.action = "destroy"
        mock_view.detail = True
        mock_request.user = self.user

        # When
        result = self.project_permissions.has_object_permission(
            mock_request, mock_view, self.project
        )

        # Then
        assert result

    def test_regular_user_cannot_delete_project(self):
        # Given
        user_project_permission = UserProjectPermission.objects.create(
            user=self.user, project=self.project
        )
        user_project_permission.permissions.add(self.read_permission)

        mock_view.action = "destroy"
        mock_view.detail = True
        mock_request.user = self.user

        # When
        result = self.project_permissions.has_object_permission(
            mock_request, mock_view, self.project
        )

        # Then
        assert not result

    def test_organisation_admin_can_perform_all_actions_on_a_project(self):
        # Given
        organisation_admin = FFAdminUser.objects.create(email="admin@test.com")
        organisation_admin.add_organisation(self.organisation, OrganisationRole.ADMIN)
        list_actions = ("list", "create")
        detail_actions = ("update", "destroy", "delete", "retrieve")
        mock_request.user = organisation_admin
        mock_request.data = {"name": "Test", "organisation": self.organisation.id}

        # When
        results = []
        for action in list_actions:
            mock_view.action = action
            mock_view.detail = False
            results.append(
                self.project_permissions.has_permission(mock_request, mock_view)
            )

        for action in detail_actions:
            mock_view.action = action
            mock_view.detail = True
            results.append(
                self.project_permissions.has_object_permission(
                    mock_request, mock_view, self.project
                )
            )

        # Then
        assert all(result for result in results)

    def test_user_with_view_project_permission_can_view_project(self):
        # Given
        user_project_permission = UserProjectPermission.objects.create(
            user=self.user, project=self.project
        )
        user_project_permission.add_permission(VIEW_PROJECT)
        mock_view.action = "retrieve"
        mock_request.user = self.user

        # When
        result = self.project_permissions.has_object_permission(
            mock_request, mock_view, self.project
        )

        # Then
        assert result


@pytest.mark.django_db
class ProjectPermissionPermissionsTestCase(TestCase):
    def setUp(self) -> None:
        organisation = Organisation.objects.create(name="Test")

        self.org_admin = FFAdminUser.objects.create(email="admin@test.com")
        self.user = FFAdminUser.objects.create(email="user@test.com")

        self.org_admin.add_organisation(organisation, OrganisationRole.ADMIN)
        self.user.add_organisation(organisation, OrganisationRole.USER)

        self.project = Project.objects.create(
            name="Test project", organisation=organisation
        )

        self.permissions = IsProjectAdmin()

        mock_view.kwargs = {"project_pk": self.project.id}

        self.read_permission = ProjectPermissionModel.objects.get(key=VIEW_PROJECT)
        self.user_project_permission = UserProjectPermission.objects.create(
            user=self.user, project=self.project
        )
        self.user_project_permission.permissions.set([self.read_permission])

    def test_project_admin_has_permission(self):
        # Given
        self.user_project_permission.admin = True
        self.user_project_permission.save()

        mock_request.user = self.user
        actions = ["list", "create"]
        mock_view.detail = False

        # When
        results = []
        for action in actions:
            mock_view.action = action
            results.append(self.permissions.has_permission(mock_request, mock_view))

        # Then
        assert all(results)

    def test_project_admin_has_object_permission(self):
        # Given
        self.user_project_permission.admin = True
        self.user_project_permission.save()

        mock_request.user = self.user
        actions = ["update", "destroy"]
        mock_view.detail = True

        # When
        results = []
        for action in actions:
            mock_view.action = action
            results.append(
                self.permissions.has_object_permission(
                    mock_request, mock_view, self.user_project_permission
                )
            )

        # Then
        assert all(results)

    def test_organisation_admin_has_permission(self):
        # Given
        mock_request.user = self.org_admin
        actions = ["list", "create"]
        mock_view.detail = False

        # When
        results = []
        for action in actions:
            mock_view.action = action
            results.append(self.permissions.has_permission(mock_request, mock_view))

        # Then
        assert all(results)

    def test_organisation_admin_has_object_permission(self):
        # Given
        mock_request.user = self.org_admin
        actions = ["update", "destroy"]
        mock_view.detail = True

        # When
        results = []
        for action in actions:
            mock_view.action = action
            results.append(
                self.permissions.has_object_permission(
                    mock_request, mock_view, self.user_project_permission
                )
            )

        # Then
        assert all(results)

    def test_regular_user_has_no_list_permission(self):
        # Given
        mock_request.user = self.user
        mock_view.action = "list"
        mock_view.detail = False

        # When
        result = self.permissions.has_permission(mock_request, mock_view)

        # Then - exception thrown
        assert not result

    def test_regular_user_has_no_create_permission(self):
        # Given
        mock_request.user = self.user
        mock_view.action = "create"
        mock_view.detail = False

        # When
        result = self.permissions.has_permission(mock_request, mock_view)

        # Then - exception thrown
        assert not result

    def test_regular_user_has_no_update_permission(self):
        # Given
        mock_request.user = self.user
        mock_view.action = "update"
        mock_view.detail = True

        # When
        result = self.permissions.has_object_permission(
            mock_request, mock_view, self.user_project_permission
        )

        # Then - exception thrown
        assert not result

    def test_regular_user_has_no_destroy_permission(self):
        # Given
        mock_request.user = self.user
        mock_view.action = "destroy"
        mock_view.detail = True

        # When
        result = self.permissions.has_object_permission(
            mock_request, mock_view, self.user_project_permission
        )

        # Then - exception thrown
        assert not result


@pytest.mark.django_db
def test_free_plan_has_only_fixed_projects_permission():
    # Given
    organisation = Organisation.objects.create(name="Test organisation")

    user = FFAdminUser.objects.create(email="admin@test.com")

    user.add_organisation(organisation, OrganisationRole.ADMIN)

    project_permissions = ProjectPermissions()

    mock_view = mock.MagicMock(action="create", detail=False)
    mock_request = mock.MagicMock(
        data={"name": "Test", "organisation": organisation.id}, user=user
    )

    # When
    for i in range(settings.MAX_PROJECTS_IN_FREE_PLAN):
        assert project_permissions.has_permission(mock_request, mock_view)
        Project.objects.create(name=f"Test project{i}", organisation=organisation)

    # Then - free projects limit should be exhausted
    assert not project_permissions.has_permission(mock_request, mock_view)


def test_is_project_admin_has_permission_raises_permission_denied_if_not_found(
    mocker, admin_user
):
    # Given
    request = mocker.MagicMock(user=admin_user)
    view = mocker.MagicMock(kwargs={"project_pk": 1})

    # Then
    with pytest.raises(PermissionDenied):
        IsProjectAdmin().has_permission(request, view)


def test_is_project_admin_has_permission_raises_api_exception_if_no_kwarg(
    mocker, admin_user
):
    # Given
    request = mocker.MagicMock(user=admin_user)
    view = mocker.MagicMock(kwargs={"foo": "bar"})

    # Then
    with pytest.raises(APIException):
        IsProjectAdmin().has_permission(request, view)


def test_is_project_admin_has_permission_returns_true_if_project_admin(
    mocker, admin_user, organisation, project
):
    # Given
    assert admin_user.is_project_admin(project)
    request = mocker.MagicMock(user=admin_user)
    view = mocker.MagicMock(kwargs={"project_pk": project.id})

    # Then
    assert IsProjectAdmin().has_permission(request, view)
