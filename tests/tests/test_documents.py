from unittest.mock import patch, Mock

from django.conf import settings
from django.db import models
from django.test import TestCase, override_settings
from django.utils.translation import gettext_lazy as _
from opensearchpy.helpers.field import GeoPoint
from opensearchpy.helpers.document import InnerDoc
from opensearchpy import OpenSearch

from django_opensearch_dsl import fields
from django_opensearch_dsl.apps import DODConfig
from django_opensearch_dsl.documents import Document
from django_opensearch_dsl.exceptions import ModelFieldNotMappedError, RedeclaredFieldError
from django_opensearch_dsl.registries import DocumentRegistry

from django_dummy_app.models import Continent
from django_dummy_app.documents import ContinentDocument

registry = DocumentRegistry()


class Car(models.Model):
    name = models.CharField(max_length=255)
    price = models.FloatField()
    not_indexed = models.TextField()
    manufacturer = models.ForeignKey("Manufacturer", null=True, on_delete=models.SET_NULL)

    class Meta:
        app_label = "car"

    def type(self):
        return "break"


class Manufacturer(models.Model):
    name = models.CharField(max_length=255)

    class Meta:
        app_label = "car"


class Article(models.Model):
    slug = models.CharField(
        max_length=255,
        unique=True,
    )

    class Meta:
        app_label = "tests"

    def __str__(self):
        return self.slug


@registry.register_document
class CarDocument(Document):
    color = fields.TextField()
    type = fields.TextField()

    def prepare_color(self, instance):
        return "blue"

    class Meta:
        doc_type = "car_document"

    class Django:
        fields = ["name", "price"]
        model = Car
        related_models = [Manufacturer]

    class Index:
        name = "car_index"
        doc_type = "car_document"
        auto_refresh = True


class DocumentTestCase(TestCase):
    fixtures = ["tests/django_dummy_app/geography_data.json"]

    def test_model_class_added(self):
        self.assertEqual(CarDocument.django.model, Car)

    def test_auto_refresh_default(self):
        self.assertTrue(CarDocument.Index.auto_refresh)

    def test_auto_refresh_added(self):
        @registry.register_document
        class CarDocument2(Document):
            class Django:
                model = Car

            class Index:
                auto_refresh = False

        self.assertFalse(CarDocument2.Index.auto_refresh)

    def test_queryset_pagination_added(self):
        @registry.register_document
        class CarDocument2(Document):
            class Django:
                model = Car
                queryset_pagination = 120

        self.assertEqual(CarDocument.django.queryset_pagination, DODConfig.default_queryset_pagination())
        self.assertEqual(CarDocument2.django.queryset_pagination, 120)

    def test_fields_populated(self):
        mapping = CarDocument._doc_type.mapping
        self.assertEqual(set(mapping.properties.properties.to_dict().keys()), {"color", "name", "price", "type"})

    def test_related_models_added(self):
        related_models = CarDocument.django.related_models
        self.assertEqual([Manufacturer], related_models)

    def test_duplicate_field_names_not_allowed(self):
        with self.assertRaises(RedeclaredFieldError):

            @registry.register_document
            class CarDocument(Document):
                color = fields.TextField()
                name = fields.TextField()

                class Django:
                    fields = ["name"]
                    model = Car

    def test_to_field(self):
        doc = Document()
        nameField = doc.to_field("name", Car._meta.get_field("name"))
        self.assertIsInstance(nameField, fields.TextField)
        self.assertEqual(nameField._path, ["name"])

    def test_to_field_with_unknown_field(self):
        doc = Document()
        with self.assertRaises(ModelFieldNotMappedError):
            doc.to_field("manufacturer", Car._meta.get_field("manufacturer"))

    def test_mapping(self):
        text_type = "text"

        self.assertEqual(
            CarDocument._doc_type.mapping.to_dict(),
            {
                "properties": {
                    "name": {"type": text_type},
                    "color": {"type": text_type},
                    "type": {"type": text_type},
                    "price": {"type": "double"},
                }
            },
        )

    def test_get_queryset(self):
        qs = CarDocument().get_queryset()
        self.assertIsInstance(qs, models.QuerySet)
        self.assertEqual(qs.model, Car)

    def test_get_indexing_queryset(self):
        doc = ContinentDocument()
        unordered_qs = doc.get_queryset().order_by("?")

        with patch("django_opensearch_dsl.documents.Document.get_queryset") as mock_qs:
            mock_qs.return_value = unordered_qs
            ordered_continents = list(doc.get_queryset().order_by("pk"))
            indexing_continents = list(doc.get_indexing_queryset())
            self.assertEqual(ordered_continents, indexing_continents)

    def test_prepare(self):
        car = Car(name="Type 57", price=5400000.0, not_indexed="not_indexex")
        doc = CarDocument()
        prepared_data = doc.prepare(car)
        self.assertEqual(
            prepared_data, {"color": doc.prepare_color(None), "type": car.type(), "name": car.name, "price": car.price}
        )

    def test_innerdoc_prepare(self):
        class ManufacturerInnerDoc(InnerDoc):
            name = fields.TextField()
            location = fields.TextField()

            def prepare_location(self, instance):
                return "USA"

        @registry.register_document
        class CarDocumentWithInnerDoc(Document):
            manufacturer = fields.ObjectField(doc_class=ManufacturerInnerDoc)

            class Django:
                model = Car
                fields = ["name", "price"]

            class Index:
                name = "car_index"

        manufacturer = Manufacturer(name="Bugatti")

        car = Car(name="Type 57", price=5400000.0, manufacturer=manufacturer)
        doc = CarDocumentWithInnerDoc()
        prepared_data = doc.prepare(car)
        self.assertEqual(
            prepared_data,
            {
                "name": car.name,
                "price": car.price,
                "manufacturer": {
                    "name": car.manufacturer.name,
                    "location": ManufacturerInnerDoc().prepare_location(manufacturer),
                },
            },
        )

    def test_prepare_ignore_dsl_base_field(self):
        @registry.register_document
        class CarDocumentDSlBaseField(Document):
            position = GeoPoint()

            class Django:
                model = Car
                fields = ["name", "price"]

            class Index:
                name = "car_index"

        car = Car(name="Type 57", price=5400000.0, not_indexed="not_indexex")
        doc = CarDocumentDSlBaseField()
        prepared_data = doc.prepare(car)
        self.assertEqual(prepared_data, {"name": car.name, "price": car.price})

    def test_model_instance_update(self):
        doc = CarDocument()
        car = Car(name="Type 57", price=5400000.0, not_indexed="not_indexex", pk=51)
        with patch("django_opensearch_dsl.documents.bulk") as mock:
            doc.update(car, "index")
            actions = [
                {
                    "_id": car.pk,
                    "_op_type": "index",
                    "_source": {
                        "name": car.name,
                        "price": car.price,
                        "type": car.type(),
                        "color": doc.prepare_color(None),
                    },
                    "_index": "car_index",
                }
            ]
            self.assertEqual(1, mock.call_count)
            self.assertEqual(actions, list(mock.call_args_list[0][1]["actions"]))
            self.assertTrue(mock.call_args_list[0][1]["refresh"])
            self.assertEqual(doc._index.connection, mock.call_args_list[0][1]["client"])

    def test_model_instance_iterable_update(self):
        doc = CarDocument()
        car = Car(name="Type 57", price=5400000.0, not_indexed="not_indexex", pk=51)
        car2 = Car(name=_("Type 42"), price=50000.0, not_indexed="not_indexex", pk=31)
        with patch("django_opensearch_dsl.documents.bulk") as mock:
            doc.update([car, car2], action="update")
            actions = [
                {
                    "_id": car.pk,
                    "_op_type": "update",
                    "doc": {
                        "name": car.name,
                        "price": car.price,
                        "type": car.type(),
                        "color": doc.prepare_color(None),
                    },
                    "_index": "car_index",
                },
                {
                    "_id": car2.pk,
                    "_op_type": "update",
                    "doc": {
                        "name": car2.name,
                        "price": car2.price,
                        "type": car2.type(),
                        "color": doc.prepare_color(None),
                    },
                    "_index": "car_index",
                },
            ]
            self.assertEqual(1, mock.call_count)
            self.assertEqual(actions, list(mock.call_args_list[0][1]["actions"]))
            self.assertTrue(mock.call_args_list[0][1]["refresh"])
            self.assertEqual(doc._index.connection, mock.call_args_list[0][1]["client"])

    def test_model_instance_update_no_refresh(self):
        doc = CarDocument()
        doc.Index.auto_refresh = False
        car = Car()
        with patch("django_opensearch_dsl.documents.bulk") as mock:
            doc.update(car, "index")
            self.assertEqual(mock.call_args_list[0][1]["refresh"], False)

    def test_model_instance_update_refresh_true(self):
        doc = CarDocument()
        doc.Index.auto_refresh = False
        car = Car()
        with patch("django_opensearch_dsl.documents.bulk") as mock:
            doc.update(car, "index", refresh=True)
            self.assertEqual(mock.call_args_list[0][1]["refresh"], True)

    def test_model_instance_update_refresh_wait_for(self):
        doc = CarDocument()
        doc.Index.auto_refresh = False
        car = Car()
        with patch("django_opensearch_dsl.documents.bulk") as mock:
            doc.update(car, "index", refresh="wait_for")
            self.assertEqual(mock.call_args_list[0][1]["refresh"], "wait_for")

    def test_model_instance_update_auto_refresh_wait_for(self):
        doc = CarDocument()
        doc.Index.auto_refresh = "wait_for"
        car = Car()
        with patch("django_opensearch_dsl.documents.bulk") as mock:
            doc.update(car, "index")
            self.assertEqual(mock.call_args_list[0][1]["refresh"], "wait_for")

    def test_model_instance_update_refresh_overrides_auto_refresh(self):
        doc = CarDocument()
        doc.Index.auto_refresh = True
        car = Car()
        with patch("django_opensearch_dsl.documents.bulk") as mock:
            doc.update(car, "index", refresh=False)
            self.assertEqual(mock.call_args_list[0][1]["refresh"], False)

    def test_model_instance_update_using(self):
        doc = CarDocument()
        car = Car()

        with patch("django_opensearch_dsl.documents.bulk") as mock:
            doc.update(car, "index")
            doc.update(car, "index", using="dummy")
            self.assertEqual(
                mock.call_args_list[0][1]["client"].transport.hosts,
                settings.OPENSEARCH_DSL["default"]["hosts"],
            )
            self.assertEqual(
                mock.call_args_list[1][1]["client"].transport.hosts,
                settings.OPENSEARCH_DSL["dummy"]["hosts"],
            )

    def test_model_instance_update_limit_fields(self):
        doc = CarDocument()
        car = Car()

        with patch("django_opensearch_dsl.documents.bulk") as mock:
            doc.update(car, "update", limit_fields=["price"])
            self.assertEqual(
                list(mock.call_args_list[0][1]["actions"])[0]["doc"],
                {"price": car.price},
            )

    def test_model_instance_iterable_update_with_pagination(self):
        class CarDocument2(Document):
            class Django:
                model = Car
                queryset_pagination = 2

        doc = CarDocument()
        car1 = Car()
        car2 = Car()
        car3 = Car()

        bulk = "django_opensearch_dsl.documents.bulk"
        parallel_bulk = "django_opensearch_dsl.documents.parallel_bulk"
        with patch(bulk) as mock_bulk, patch(parallel_bulk) as mock_parallel_bulk:
            doc.update([car1, car2, car3], "index")
            self.assertEqual(3, len(list(mock_bulk.call_args_list[0][1]["actions"])))
            self.assertEqual(mock_bulk.call_count, 1, "bulk is called")
            self.assertEqual(mock_parallel_bulk.call_count, 0, "parallel bulk is not called")

    def test_model_instance_iterable_update_with_parallel(self):
        class CarDocument2(Document):
            class Django:
                model = Car

        doc = CarDocument()
        car1 = Car()
        car2 = Car()
        car3 = Car()
        bulk = "django_opensearch_dsl.documents.bulk"
        parallel_bulk = "django_opensearch_dsl.documents.parallel_bulk"
        with patch(bulk) as mock_bulk, patch(parallel_bulk) as mock_parallel_bulk:
            doc.update([car1, car2, car3], "index", parallel=True)
            self.assertEqual(mock_bulk.call_count, 0, "bulk is not called")
            self.assertEqual(mock_parallel_bulk.call_count, 1, "parallel bulk is called")

    def test_init_prepare_correct(self):
        """Run init_prepare() run and collect the right preparation functions"""

        d = CarDocument()
        self.assertEqual(len(d._prepared_fields), 4)

        expect = {
            "color": (
                "<class 'django_opensearch_dsl.fields.TextField'>",
                ("<class 'method'>", "<type 'instancemethod'>"),
            ),  # py3, py2
            "type": (
                "<class 'django_opensearch_dsl.fields.TextField'>",
                ("<class 'functools.partial'>", "<type 'functools.partial'>"),
            ),
            "name": (
                "<class 'django_opensearch_dsl.fields.TextField'>",
                ("<class 'functools.partial'>", "<type 'functools.partial'>"),
            ),
            "price": (
                "<class 'django_opensearch_dsl.fields.DoubleField'>",
                ("<class 'functools.partial'>", "<type 'functools.partial'>"),
            ),
        }

        for name, field, prep in d._prepared_fields:
            e = expect[name]
            self.assertEqual(str(type(field)), e[0], "field type should be copied over")
            self.assertTrue("__call__" in dir(prep), "prep function should be callable")
            self.assertTrue(str(type(prep)) in e[1], "prep function is correct partial or method")

    def test_init_prepare_results(self):
        """Are the results from init_prepare() actually used in prepare()?"""
        d = CarDocument()

        car = Car()
        setattr(car, "name", "Tusla")
        setattr(car, "price", 340123.21)
        setattr(car, "color", "polka-dots")  # Overwritten by prepare function
        setattr(car, "pk", 4701)  # Ignored, not in document
        setattr(car, "type", "imaginary")

        self.assertEqual(d.prepare(car), {"color": "blue", "type": "imaginary", "name": "Tusla", "price": 340123.21})

        m = Mock()
        # This will blow up should we access _fields and try to iterate over it.
        # Since init_prepare compiles a list of prepare functions, while
        # preparing no access to _fields should happen
        with patch.object(CarDocument, "_fields", 33):
            d.prepare(m)
        self.assertEqual(
            sorted([tuple(x) for x in m.method_calls], key=lambda _: _[0]),
            [("name", (), {}), ("price", (), {}), ("type", (), {})],
        )

    # Mock the opensearch connection because we need to execute the bulk so that
    # the generator got iterated and generate_id called.
    # If we mock the bulk in django_opensearch_dsl.document
    # the actual bulk will be never called and the test will fail
    @patch("opensearchpy.helpers.actions.bulk")
    def test_default_generate_id_is_called(self, _):
        article = Article(
            id=124594,
            slug="some-article",
        )

        @registry.register_document
        class ArticleDocument(Document):
            class Django:
                model = Article
                fields = [
                    "slug",
                ]

            class Index:
                name = "test_articles"
                settings = {
                    "number_of_shards": 1,
                    "number_of_replicas": 0,
                }

        with patch.object(ArticleDocument, "generate_id", return_value=article.id) as patched_method:
            d = ArticleDocument()
            d.update(article, "index")
            patched_method.assert_called()

    @patch("django_opensearch_dsl.documents.Document.bulk")
    def test_custom_generate_id_is_called(self, mock_bulk):
        article = Article(
            id=54218,
            slug="some-article-2",
        )

        @registry.register_document
        class ArticleDocument(Document):
            class Django:
                model = Article
                fields = [
                    "slug",
                ]

            class Index:
                name = "test_articles"

            @classmethod
            def generate_id(cls, article):
                return article.slug

        d = ArticleDocument()
        d.update(article, "index", refresh=True)

        # Get the data from the opensearch low level API because
        # The generator get executed there.
        assert list(mock_bulk.call_args[0][0])[0]["_id"] == article.slug

    @override_settings(OPENSEARCH_DSL_INDEX_SETTINGS={"codec": "best_compression"})
    def test_index_settings_default_to_settings(self):
        @registry.register_document
        class ArticleDocument(Document):
            class Django:
                model = Article
                fields = [
                    "slug",
                ]

            class Index:
                name = "test_articles"

        self.assertEqual(ArticleDocument._index._settings, {"codec": "best_compression"})

    @override_settings(OPENSEARCH_DSL_INDEX_SETTINGS={"codec": "best_compression"})
    def test_index_settings_use_index_settings_merge_global_settings(self):
        @registry.register_document
        class ArticleDocument(Document):
            class Django:
                model = Article
                fields = [
                    "slug",
                ]

            class Index:
                name = "test_articles"
                settings = {"hidden": True}

        self.assertEqual(ArticleDocument._index._settings, {"codec": "best_compression", "hidden": True})

    @override_settings(OPENSEARCH_DSL_INDEX_SETTINGS={"codec": "best_compression"})
    def test_index_settings_use_index_settings_merge_global_settings(self):
        @registry.register_document
        class ArticleDocument(Document):
            class Django:
                model = Article
                fields = [
                    "slug",
                ]

            class Index:
                name = "test_articles"
                settings = {"hidden": True}

        self.assertEqual(ArticleDocument._index._settings, {"codec": "best_compression", "hidden": True})

    @override_settings(OPENSEARCH_DSL_INDEX_SETTINGS={"codec": "best_compression"})
    def test_index_settings_use_index_settings_override_global_settings(self):
        @registry.register_document
        class ArticleDocument(Document):
            class Django:
                model = Article
                fields = [
                    "slug",
                ]

            class Index:
                name = "test_articles"
                settings = {
                    "hidden": True,
                    "codec": "default",
                }

        self.assertEqual(ArticleDocument._index._settings, {"codec": "default", "hidden": True})
